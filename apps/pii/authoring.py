"""Flag-gated placeholder-at-rest authoring chokepoint.

Every Layer-1 writer supplies its provenance class here. The returned receipt
is stored beside the authored field; callers never infer cleanliness from the
text alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal

from apps.pii.config import TIER_POLICIES
from apps.pii.egress import redact_known_values
from apps.pii.entity_registry import canonical_key, get_name, inverted_names_ci, is_denied
from apps.pii.redactor import (
    _PLACEHOLDER_RE,
    MINT_ALL,
    MINT_NEVER,
    MINT_VALIDATED,
    _detect_pii,
    _filter_results,
    redact_user_message,
    redact_user_message_checked,
)

logger = logging.getLogger(__name__)

WriterClass = Literal["owner", "runtime", "background"]
_WRITER_POLICIES = {
    "owner": (MINT_ALL, True),
    "runtime": (MINT_NEVER, False),
    "background": (MINT_VALIDATED, False),
}
_RESIDUAL_KINDS = frozenset({"PERSON", "LOCATION"})


@dataclass(frozen=True)
class AuthoredText:
    """Stored text plus its offset-free, per-field provenance receipt."""

    text: str
    receipt: dict[str, Any]


def placeholder_redactions(text: str, entity_map: dict | None) -> list[dict[str, str | None]]:
    """Return chat-parity ``{placeholder, value}`` metadata in appearance order."""
    if not text:
        return []
    entity_map = entity_map or {}
    out: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for match in _PLACEHOLDER_RE.finditer(text):
        placeholder = f"[{match.group(1)}_{match.group(2)}]"
        if placeholder in seen:
            continue
        seen.add(placeholder)
        name = get_name(entity_map.get(placeholder))
        out.append({"placeholder": placeholder, "value": name or None})
    return out


def truncate_placeholder_safe(text: str, max_len: int) -> str:
    """Truncate without leaving a partial ``[TYPE_N]`` placeholder token."""
    if max_len < 0:
        raise ValueError("max_len must be non-negative")
    if len(text) <= max_len:
        return text
    for match in _PLACEHOLDER_RE.finditer(text):
        if match.start() < max_len < match.end():
            return text[: match.start()]
    return text[:max_len]


def _residual_summary(tenant, text: str) -> dict[str, Any]:
    """Count unknown PERSON/LOCATION detections without retaining their values."""
    tier = getattr(tenant, "model_tier", "starter")
    policy = TIER_POLICIES.get(tier, TIER_POLICIES["starter"])
    results = _detect_pii(
        text,
        policy.get("entities", []),
        policy.get("score_threshold", 0.7),
    )
    denylist = getattr(tenant, "pii_denylist", None) or {}
    results = _filter_results(results, text, set(), denylist=denylist, tenant=tenant)
    placeholder_ranges = [(match.start(), match.end()) for match in _PLACEHOLDER_RE.finditer(text)]
    known = inverted_names_ci(getattr(tenant, "pii_entity_map", None) or {})

    kinds: dict[str, int] = {}
    for result in results:
        if result.entity_type not in _RESIDUAL_KINDS:
            continue
        if any(result.start < end and start < result.end for start, end in placeholder_ranges):
            continue
        value_key = canonical_key(text[result.start : result.end])
        if value_key and value_key in known:
            continue
        kinds[result.entity_type] = kinds.get(result.entity_type, 0) + 1
    return {"count": sum(kinds.values()), "kinds": kinds}


def _log_counter(*, tenant, seam: str, writer: str, field: str, state: str) -> None:
    logger.info(
        "pii_authoring_counter tenant=%s seam=%s writer=%s field=%s state=%s count=1",
        getattr(tenant, "id", "?"),
        seam,
        writer,
        field,
        state,
    )


def _redact_active_known_values(tenant, text: str, *, seam: str) -> str:
    """Apply the independent known-value path without retired bindings."""
    entity_map = getattr(tenant, "pii_entity_map", None) or {}
    denylist = getattr(tenant, "pii_denylist", None) or {}
    active_map = {
        placeholder: entry
        for placeholder, entry in entity_map.items()
        if not (isinstance(entry, dict) and entry.get("retired")) and not is_denied(denylist, get_name(entry))
    }
    if len(active_map) == len(entity_map):
        return redact_known_values(tenant, text, seam=seam)
    active_tenant = SimpleNamespace(
        id=getattr(tenant, "id", None),
        pk=getattr(tenant, "pk", None),
        pii_entity_map=active_map,
    )
    return redact_known_values(active_tenant, text, seam=seam)


def author_text(
    tenant,
    text: str,
    *,
    seam: str,
    writer: WriterClass,
    field: str,
) -> AuthoredText:
    """Author one text field under its writer-class mint policy.

    Flag-off preserves the pre-P3 behavior of each writer class. Owner writes
    still use the legacy unchecked redactor; runtime/background writes remain
    byte-identical passthroughs.
    """
    if writer not in _WRITER_POLICIES:
        raise ValueError(f"unsupported writer class: {writer!r}")

    if not getattr(tenant, "layer1_placeholder_writes", False):
        if writer == "owner":
            text = redact_user_message(text, tenant)
            receipt = {"state": "bypass", "mode": "legacy-redact"}
        else:
            receipt = {"state": "bypass"}
        _log_counter(tenant=tenant, seam=seam, writer=writer, field=field, state="bypass")
        return AuthoredText(text=text, receipt=receipt)

    mint, allow_user_name = _WRITER_POLICIES[writer]
    try:
        outcome = redact_user_message_checked(
            text,
            tenant,
            allow_user_name=allow_user_name,
            mint=mint,
        )
    except Exception:
        logger.exception(
            "pii_authoring_redaction_error tenant=%s seam=%s writer=%s field=%s",
            getattr(tenant, "id", "?"),
            seam,
            writer,
            field,
        )
        outcome = None

    reason = getattr(outcome, "reason", "redaction-error")
    if reason == "empty-input":
        receipt = {"state": "placeholder", "reason": reason, "redactions": []}
        _log_counter(tenant=tenant, seam=seam, writer=writer, field=field, state="placeholder")
        return AuthoredText(text=text, receipt=receipt)
    if reason == "redaction-disabled":
        receipt = {"state": "bypass", "reason": reason}
        _log_counter(tenant=tenant, seam=seam, writer=writer, field=field, state="bypass")
        return AuthoredText(text=text, receipt=receipt)
    if outcome is None or not outcome.confirmed:
        stored = _redact_active_known_values(tenant, text, seam=f"{seam}:known-fallback")
        receipt = {
            "state": "unconfirmed",
            "reason": "redaction-error",
            "redactions": placeholder_redactions(stored, getattr(tenant, "pii_entity_map", None)),
        }
        _log_counter(tenant=tenant, seam=seam, writer=writer, field=field, state="unconfirmed")
        return AuthoredText(text=stored, receipt=receipt)

    stored = outcome.text
    if writer in {"runtime", "background"}:
        stored = _redact_active_known_values(tenant, stored, seam=f"{seam}:known-values")

    receipt: dict[str, Any] = {
        "state": "placeholder",
        "redactions": placeholder_redactions(stored, getattr(tenant, "pii_entity_map", None)),
    }
    if writer == "background":
        try:
            residual_spans = _residual_summary(tenant, text)
        except Exception:
            logger.exception(
                "pii_authoring_residual_detection_error tenant=%s seam=%s field=%s",
                getattr(tenant, "id", "?"),
                seam,
                field,
            )
            stored = _redact_active_known_values(tenant, stored, seam=f"{seam}:known-fallback")
            receipt = {
                "state": "unconfirmed",
                "reason": "redaction-error",
                "redactions": placeholder_redactions(stored, getattr(tenant, "pii_entity_map", None)),
            }
        else:
            if residual_spans["count"]:
                receipt["state"] = "residual"
                receipt["residual_spans"] = residual_spans

    _log_counter(tenant=tenant, seam=seam, writer=writer, field=field, state=receipt["state"])
    return AuthoredText(text=stored, receipt=receipt)
