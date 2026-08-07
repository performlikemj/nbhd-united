"""Deterministic known-value protection for storage and model egress seams."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from apps.pii.entity_registry import get_metadata, inverted_names_ci, iter_normalized
from apps.pii.redactor import _PLACEHOLDER_RE

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

ENTITY_LEGEND_HEADER = "[Entity legend — placeholder-space, for context only]"
_ENTITY_LEGEND_MAX_ENTRIES = 20
_ENTITY_LEGEND_MAX_LINE_CHARS = 140


@dataclass(frozen=True)
class _KnownValueMatcher:
    pattern: re.Pattern[str]
    placeholders: dict[str, str]


def _edge_boundary(character: str) -> str:
    return r"\b" if character and (character.isalnum() or character == "_") else ""


def _active_entity_map(entity_map: dict[str, Any]) -> dict[str, Any]:
    """Return bindings eligible for egress substitution and legends."""
    return {
        placeholder: entry
        for placeholder, entry in entity_map.items()
        if not (isinstance(entry, dict) and entry.get("retired"))
    }


@lru_cache(maxsize=512)
def _compile_known_value_matcher(
    tenant_key: str,
    map_hash: str,
    bindings: tuple[tuple[str, str], ...],
) -> _KnownValueMatcher | None:
    """Compile one longest-first alternation for a tenant/map snapshot.

    ``tenant_key`` and ``map_hash`` intentionally participate in the cache key.
    The bindings make hash collisions harmless and let tests prove invalidation.
    """
    del tenant_key, map_hash
    if not bindings:
        return None

    alternatives: list[str] = []
    placeholders: dict[str, str] = {}
    for value, placeholder in bindings:
        alternatives.append(f"{_edge_boundary(value[:1])}{re.escape(value)}{_edge_boundary(value[-1:])}")
        placeholders[value.casefold()] = placeholder
    return _KnownValueMatcher(
        pattern=re.compile("(?:" + "|".join(alternatives) + ")", re.IGNORECASE),
        placeholders=placeholders,
    )


def _matcher_for_tenant(tenant: Tenant) -> _KnownValueMatcher | None:
    entity_map = _active_entity_map(getattr(tenant, "pii_entity_map", None) or {})
    canonical: dict[str, tuple[str, str]] = {}
    for key, (display_name, placeholder) in inverted_names_ci(entity_map).items():
        value = display_name.strip()
        # This guard is deliberately stricter than inbound/CJK detection: the
        # ratified egress contract excludes every mapped value under 3 chars.
        if len(value) < 3:
            continue
        canonical[key] = (value, placeholder)

    bindings = tuple(sorted(canonical.values(), key=lambda item: (-len(item[0]), item[0].casefold(), item[1])))
    digest = hashlib.sha256(repr(bindings).encode("utf-8")).hexdigest()
    tenant_key = str(getattr(tenant, "pk", None) or getattr(tenant, "id", None) or id(tenant))
    return _compile_known_value_matcher(tenant_key, digest, bindings)


def _redact_known_values(tenant: Tenant | None, text: str) -> str:
    if not text or tenant is None:
        return text
    matcher = _matcher_for_tenant(tenant)
    if matcher is None:
        return text

    def replace(match: re.Match[str]) -> str:
        return matcher.placeholders[match.group(0).casefold()]

    parts: list[str] = []
    last = 0
    for placeholder_match in _PLACEHOLDER_RE.finditer(text):
        parts.append(matcher.pattern.sub(replace, text[last : placeholder_match.start()]))
        parts.append(placeholder_match.group(0))
        last = placeholder_match.end()
    parts.append(matcher.pattern.sub(replace, text[last:]))
    return "".join(parts)


def redact_known_values(tenant: Tenant | None, text: str, *, seam: str) -> str:
    """Replace mapped tenant values with canonical placeholders, fail-open.

    The transform performs no NER, mints nothing, preserves existing placeholder
    interiors, skips values shorter than three characters, and uses a compiled
    per-tenant longest-first alternation cached by the entity-map hash.
    """
    try:
        return _redact_known_values(tenant, text)
    except Exception:
        logger.warning(
            "pii_egress_guard_error tenant=%s seam=%s",
            getattr(tenant, "id", "?"),
            seam,
            exc_info=True,
        )
        return text


def build_entity_legend(tenant: Tenant | None, text: str) -> str:
    """Describe placeholders present in ``text`` using safe registry metadata.

    Relationship and notes fields are user-authored, so their combined text is
    passed through the same known-value redactor as model-bound content. The
    returned body has no framing header; callers use ``append_entity_legend``
    (or ``entity_legend_block`` for structured prompts) to add it fail-open.
    """
    if not text or tenant is None:
        return ""
    entity_map = getattr(tenant, "pii_entity_map", None) or {}
    if not entity_map:
        return ""

    present = {f"[{match.group(1)}_{match.group(2)}]" for match in _PLACEHOLDER_RE.finditer(text)}
    if not present:
        return ""

    entries = dict(iter_normalized(_active_entity_map(entity_map)))
    matcher = _matcher_for_tenant(tenant)
    ordered = sorted(
        present,
        key=lambda placeholder: (
            int(placeholder.rsplit("_", 1)[1][:-1]),
            placeholder,
        ),
    )

    lines: list[str] = []
    for placeholder in ordered:
        if placeholder not in entries:
            continue
        meta = get_metadata(entries.get(placeholder))
        relationship_value = meta.get("relationship")
        notes_value = meta.get("notes")
        relationship = " ".join(relationship_value.split()) if isinstance(relationship_value, str) else ""
        notes = " ".join(notes_value.split()) if isinstance(notes_value, str) else ""
        if not relationship and not notes:
            continue

        descriptor = _redact_known_values(
            tenant,
            "; ".join(part for part in (relationship, notes) if part),
        )
        assert matcher is None or matcher.pattern.search(descriptor) is None, (
            "mapped raw value survived entity legend redaction"
        )
        lines.append(f"{placeholder}: {descriptor}"[:_ENTITY_LEGEND_MAX_LINE_CHARS].rstrip())
        if len(lines) >= _ENTITY_LEGEND_MAX_ENTRIES:
            break

    return "\n".join(lines)


def entity_legend_block(tenant: Tenant | None, text: str, *, seam: str) -> str:
    """Return a framed entity legend block, logging and omitting on failure."""
    try:
        legend = build_entity_legend(tenant, text)
    except Exception:
        logger.warning(
            "pii_egress_guard_error tenant=%s seam=%s",
            getattr(tenant, "id", "?"),
            f"legend:{seam}",
            exc_info=True,
        )
        return ""
    if not legend:
        return ""
    return f"\n\n{ENTITY_LEGEND_HEADER}\n{legend}"


def append_entity_legend(tenant: Tenant | None, text: str, *, seam: str) -> str:
    """Append a contextual entity legend without changing empty-legend text."""
    return text + entity_legend_block(tenant, text, seam=seam)


def redact_known_value_fields(
    tenant: Tenant | None,
    payload: Any,
    *,
    seam: str,
    text_fields: frozenset[str],
) -> Any:
    """Recursively guard allowlisted human-text fields in a JSON-like payload."""
    try:

        def walk(value: Any, redact_strings: bool = False) -> Any:
            if isinstance(value, str):
                return _redact_known_values(tenant, value) if redact_strings else value
            if isinstance(value, list):
                return [walk(item, redact_strings) for item in value]
            if isinstance(value, dict):
                return {key: walk(item, redact_strings or str(key) in text_fields) for key, item in value.items()}
            return value

        return walk(payload)
    except Exception:
        logger.warning(
            "pii_egress_guard_error tenant=%s seam=%s",
            getattr(tenant, "id", "?"),
            seam,
            exc_info=True,
        )
        return payload


class KnownValueResponseGuardMixin:
    """DRF view mixin that guards allowlisted response fields before rendering."""

    pii_egress_seam = "runtime_response"
    pii_egress_text_fields: frozenset[str] = frozenset()

    def finalize_response(self, request, response, *args, **kwargs):
        tenant_id = kwargs.get("tenant_id") or getattr(self, "kwargs", {}).get("tenant_id")
        if tenant_id and hasattr(response, "data"):
            try:
                from apps.tenants.models import Tenant

                tenant = Tenant.objects.filter(pk=tenant_id).only("id", "pii_entity_map").first()
                response.data = redact_known_value_fields(
                    tenant,
                    response.data,
                    seam=self.pii_egress_seam,
                    text_fields=self.pii_egress_text_fields,
                )
            except Exception:
                logger.warning(
                    "pii_egress_guard_error tenant=%s seam=%s",
                    tenant_id,
                    self.pii_egress_seam,
                    exc_info=True,
                )
        return super().finalize_response(request, response, *args, **kwargs)
