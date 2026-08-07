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


def receipt_placeholders(text: str) -> list[dict[str, str]]:
    """Return placeholder-only receipt metadata in first-appearance order."""
    if not text:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _PLACEHOLDER_RE.finditer(text):
        placeholder = f"[{match.group(1)}_{match.group(2)}]"
        if placeholder in seen:
            continue
        seen.add(placeholder)
        out.append({"placeholder": placeholder})
    return out


def resolve_receipt_values(receipts: Any, entity_map: dict | None) -> dict[str, Any]:
    """Resolve new and legacy receipt shapes against the current entity map.

    Persisted receipt values are never trusted: a renamed live binding wins
    over an embedded W1c canary value. A placeholder the live map cannot
    resolve (unbound or tombstoned) emits NO ``value`` key at all rather than
    an explicit null — an absent key and a null both decode to "unknown"
    downstream, and omitting it keeps a stale embedded value from surviving.
    """
    if not isinstance(receipts, dict):
        return {}
    entity_map = entity_map or {}
    resolved: dict[str, Any] = {}
    for field, raw_receipt in receipts.items():
        if not isinstance(raw_receipt, dict):
            resolved[field] = raw_receipt
            continue
        receipt = dict(raw_receipt)
        redactions = raw_receipt.get("redactions")
        if isinstance(redactions, list):
            next_redactions = []
            for raw_item in redactions:
                if not isinstance(raw_item, dict):
                    next_redactions.append(raw_item)
                    continue
                item = dict(raw_item)
                placeholder = item.get("placeholder")
                if isinstance(placeholder, str):
                    name = get_name(entity_map.get(placeholder))
                    if name:
                        item["value"] = name
                    else:
                        item.pop("value", None)
                next_redactions.append(item)
            receipt["redactions"] = next_redactions
        resolved[field] = receipt
    return resolved


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


def _registered_field_max_length(field: str) -> int | None:
    """Return the strictest registered model limit for a flat text field."""
    from apps.pii.store_registry import registered_stores

    limits = []
    for store in registered_stores():
        if field not in store.flat_fields:
            continue
        max_length = getattr(store.model._meta.get_field(field), "max_length", None)
        if max_length is not None:
            limits.append(max_length)
    return min(limits) if limits else None


def _residual_summary(tenant, text: str) -> dict[str, Any]:
    """Count unknown PERSON/LOCATION detections without retaining their values.

    ``text`` must be the STORED text, not the pre-redaction input. The receipt
    describes what is at rest, and detected spans are matched against known
    bindings by exact value — on raw input the detector regularly over-captures
    ("Call Alice" comes back as one PERSON span), so the known-value lookup
    misses and a fully-redacted field is recorded as residual forever.
    """
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


def _finalize(
    tenant,
    text: str,
    receipt: dict[str, Any],
    *,
    seam: str,
    writer: WriterClass,
    field: str,
    checked: bool,
    live: bool = True,
    source_text: str | None = None,
) -> AuthoredText:
    """Apply invariants shared by every authoring outcome before persistence.

    Truncation covers PLACEHOLDER GROWTH only: authoring can make text longer
    (a short name becomes ``[PERSON_12]``) and an authored overflow would raise
    a DB error nobody asked for. Text the caller sent over the limit already is
    left alone so serializer validation still answers it with a 400, exactly as
    it did pre-P3. Passing ``source_text=None`` opts out entirely (bypass paths
    must stay byte-identical).
    """
    if source_text is not None:
        max_length = _registered_field_max_length(field)
        if max_length is not None and len(source_text) <= max_length:
            text = truncate_placeholder_safe(text, max_length)

    receipt = dict(receipt)
    receipt["writer"] = writer
    if "redactions" in receipt:
        receipt["redactions"] = receipt_placeholders(text)

    state = receipt["state"]
    _log_counter(tenant=tenant, seam=seam, writer=writer, field=field, state=state)
    if checked and live:
        from apps.pii.alerts import record_live_write_outcome

        record_live_write_outcome(
            tenant,
            seam=seam,
            writer=writer,
            is_error=state == "unconfirmed",
        )
    return AuthoredText(text=text, receipt=receipt)


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
    live: bool = True,
) -> AuthoredText:
    """Author one text field under its writer-class mint policy.

    Flag-off preserves the pre-P3 behavior of each writer class. Owner writes
    still use the legacy unchecked redactor; runtime/background writes remain
    byte-identical passthroughs — including length, so a bypass never truncates.

    ``live=False`` marks a re-authoring pass over already-stored rows (the
    repair sweep). It keeps such passes out of the live-write error-rate
    counters, which exist to measure what real user writes are experiencing.
    """
    if writer not in _WRITER_POLICIES:
        raise ValueError(f"unsupported writer class: {writer!r}")

    if not getattr(tenant, "layer1_placeholder_writes", False):
        if writer == "owner":
            text = redact_user_message(text, tenant)
            receipt = {"state": "bypass", "mode": "legacy-redact"}
        else:
            receipt = {"state": "bypass"}
        return _finalize(
            tenant,
            text,
            receipt,
            seam=seam,
            writer=writer,
            field=field,
            checked=False,
            live=live,
        )

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
        return _finalize(
            tenant,
            text,
            receipt,
            seam=seam,
            writer=writer,
            field=field,
            checked=False,
            live=live,
            source_text=text,
        )
    if reason == "redaction-disabled":
        receipt = {"state": "bypass", "reason": reason}
        return _finalize(
            tenant,
            text,
            receipt,
            seam=seam,
            writer=writer,
            field=field,
            checked=False,
            live=live,
        )
    if outcome is None or not outcome.confirmed:
        stored = _redact_active_known_values(tenant, text, seam=f"{seam}:known-fallback")
        receipt = {
            "state": "unconfirmed",
            "reason": "redaction-error",
            "redactions": [],
        }
        return _finalize(
            tenant,
            stored,
            receipt,
            seam=seam,
            writer=writer,
            field=field,
            checked=True,
            live=live,
            source_text=text,
        )

    stored = outcome.text
    if writer in {"runtime", "background"}:
        stored = _redact_active_known_values(tenant, stored, seam=f"{seam}:known-values")

    receipt: dict[str, Any] = {
        "state": "placeholder",
        "redactions": [],
    }
    if writer in {"runtime", "background"}:
        # Runtime never mints, so detection is the only thing standing between a
        # model-composed raw name and a receipt that reads clean forever: the A7
        # migration fence trusts `placeholder` and the repair sweep only revisits
        # unconfirmed/residual.
        try:
            residual_spans = _residual_summary(tenant, stored)
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
                "redactions": [],
            }
        else:
            if residual_spans["count"]:
                receipt["state"] = "residual"
                receipt["residual_spans"] = residual_spans

    return _finalize(
        tenant,
        stored,
        receipt,
        seam=seam,
        writer=writer,
        field=field,
        checked=True,
        live=live,
        source_text=text,
    )
