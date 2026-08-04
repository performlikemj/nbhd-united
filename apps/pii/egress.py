"""Deterministic known-value protection for storage and model egress seams."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from apps.pii.entity_registry import inverted_names_ci
from apps.pii.redactor import _PLACEHOLDER_RE

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _KnownValueMatcher:
    pattern: re.Pattern[str]
    placeholders: dict[str, str]


def _edge_boundary(character: str) -> str:
    return r"\b" if character and (character.isalnum() or character == "_") else ""


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
    entity_map = getattr(tenant, "pii_entity_map", None) or {}
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


def redact_known_values(tenant: Tenant | None, text: str, *, seam: str) -> str:
    """Replace mapped tenant values with canonical placeholders, fail-open.

    The transform performs no NER, mints nothing, preserves existing placeholder
    interiors, skips values shorter than three characters, and uses a compiled
    per-tenant longest-first alternation cached by the entity-map hash.
    """
    if not text or tenant is None:
        return text
    try:
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
    except Exception:
        logger.warning(
            "pii_egress_guard_error tenant=%s seam=%s",
            getattr(tenant, "id", "?"),
            seam,
            exc_info=True,
        )
        return text


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
                return redact_known_values(tenant, value, seam=seam) if redact_strings else value
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
