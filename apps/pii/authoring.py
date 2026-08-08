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


@dataclass(frozen=True)
class AuthoredJSON:
    """A rewritten JSON value plus one aggregate receipt for its model field."""

    value: Any
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
    over an embedded W1c canary value. Only a placeholder with NO live binding
    omits the ``value`` key entirely rather than emitting an explicit null — an
    absent key and a null both decode to "unknown" downstream, and omitting it
    keeps a stale embedded value from surviving.

    A retired (tombstoned) binding still resolves to its name, deliberately:
    the binding stays for rehydration, so the receipt matches what the owner is
    actually shown.
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


def _registered_field_max_length(field: str, model_label: str | None = None) -> int | None:
    """Return the registered column limit for a flat text field.

    ``model_label`` resolves the limit against ONE store, which is the only
    correct answer once two stores register the same field NAME with different
    limits — a name-global minimum would silently truncate the roomier store's
    writes to the tighter store's column. Callers that know which model they are
    writing should always pass it.

    Omitting it is retained for pre-registry callers. A name-only lookup applies
    a limit only when every registered namesake agrees; ambiguity returns no
    central cap so authoring cannot silently truncate for an unrelated model.
    The owning serializer/model remains responsible for its normal validation,
    and registered writers should pass ``model_label`` for exact safe growth
    truncation.
    """
    from apps.pii.store_registry import registered_stores

    limits: set[int | None] = set()
    for store in registered_stores():
        if field not in store.flat_fields:
            continue
        if model_label is not None and store.model_label != model_label:
            continue
        max_length = getattr(store.model._meta.get_field(field), "max_length", None)
        limits.add(max_length)
    if len(limits) != 1:
        return None
    return next(iter(limits))


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
    model_label: str | None = None,
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
        max_length = _registered_field_max_length(field, model_label)
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
    model_label: str | None = None,
    flag_off_legacy_redaction: bool = True,
) -> AuthoredText:
    """Author one text field under its writer-class mint policy.

    Flag-off preserves the pre-P3 behavior of each writer seam. Owner writes
    use the legacy unchecked redactor by default; newly routed owner seams that
    were raw before P3 pass ``flag_off_legacy_redaction=False`` for a byte-
    identical bypass. Runtime/background writes remain byte-identical
    passthroughs — including length, so a bypass never truncates.

    ``live=False`` marks a re-authoring pass over already-stored rows (the
    repair sweep). It keeps such passes out of the live-write error-rate
    counters, which exist to measure what real user writes are experiencing.

    ``model_label`` names the store being written (``"journal.Document"``) so
    the post-authoring length cap resolves against THAT column rather than the
    strictest column sharing the field name — see
    :func:`_registered_field_max_length`.

    ``flag_off_legacy_redaction`` is ONLY an A4 compatibility switch. It does
    not alter flag-on policy: owner text still runs full checked authoring with
    ``MINT_ALL``.
    """
    if writer not in _WRITER_POLICIES:
        raise ValueError(f"unsupported writer class: {writer!r}")

    if not getattr(tenant, "layer1_placeholder_writes", False):
        source_text = None
        if writer == "owner" and flag_off_legacy_redaction:
            # The legacy redactor substitutes placeholders, so this branch was
            # never byte-identical to its input and it grows text the same way
            # the checked path does — a short name becoming ``[PERSON_12]`` can
            # push a title past its column and 500 on the insert. It gets the
            # same growth-only cap: ``source_text`` opts in, so text the caller
            # sent over the limit already is still left alone for serializer
            # validation to answer with a 400.
            source_text = text
            text = redact_user_message(text, tenant)
            receipt = {"state": "bypass", "mode": "legacy-redact"}
        else:
            # Runtime/background flag-off stays a pure passthrough, length
            # included — a bypass must never truncate.
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
            source_text=source_text,
            model_label=model_label,
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
            model_label=model_label,
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
            model_label=model_label,
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
        model_label=model_label,
    )


_JSON_RECEIPT_STATE_RANK = {"unconfirmed": 0, "residual": 1, "bypass": 2, "placeholder": 3}


def _aggregate_json_receipts(receipts: list[dict[str, Any]], *, writer: WriterClass) -> dict[str, Any]:
    """Fold leaf receipts into one A2 receipt keyed by the JSONField name."""
    winner = min(
        receipts,
        key=lambda receipt: _JSON_RECEIPT_STATE_RANK.get(receipt.get("state"), -1),
    )
    aggregate = dict(winner)
    aggregate["writer"] = writer

    if aggregate.get("state") == "bypass":
        aggregate.pop("redactions", None)
        aggregate.pop("residual_spans", None)
        return aggregate

    redactions: list[dict[str, str]] = []
    seen: set[str] = set()
    for receipt in receipts:
        for item in receipt.get("redactions", []):
            placeholder = item.get("placeholder") if isinstance(item, dict) else None
            if isinstance(placeholder, str) and placeholder not in seen:
                seen.add(placeholder)
                redactions.append({"placeholder": placeholder})
    aggregate["redactions"] = redactions

    if aggregate.get("state") == "residual":
        kinds: dict[str, int] = {}
        for receipt in receipts:
            summary = receipt.get("residual_spans")
            if not isinstance(summary, dict) or not isinstance(summary.get("kinds"), dict):
                continue
            for kind, count in summary["kinds"].items():
                if isinstance(count, int):
                    kinds[kind] = kinds.get(kind, 0) + count
        aggregate["residual_spans"] = {"count": sum(kinds.values()), "kinds": kinds}
    else:
        aggregate.pop("residual_spans", None)
    return aggregate


def author_json_paths(
    tenant,
    value: Any,
    *,
    paths: tuple[tuple[str, ...], ...],
    seam: str,
    writer: WriterClass,
    field: str,
    live: bool = True,
    model_label: str | None = None,
    flag_off_legacy_redaction: bool = True,
) -> AuthoredJSON:
    """Author every string leaf selected by parsed registry path suffixes.

    Each leaf goes through :func:`author_text`; the returned receipt aggregates
    state, residual counts, and placeholder metadata at the top-level JSONField
    boundary required by directive A2.
    """
    from apps.pii.store_registry import rewrite_json_path

    leaf_receipts: list[dict[str, Any]] = []

    def _author_leaf(text: str) -> str:
        authored = author_text(
            tenant,
            text,
            seam=seam,
            writer=writer,
            field=field,
            live=live,
            model_label=model_label,
            flag_off_legacy_redaction=flag_off_legacy_redaction,
        )
        leaf_receipts.append(authored.receipt)
        return authored.text

    authored_value = value
    for path in paths:
        authored_value, _changed = rewrite_json_path(authored_value, path, _author_leaf)

    if not leaf_receipts:
        recursive_payload = any(path and path[-1] == "**" for path in paths)
        if value is None or value == "" or value == [] or value == {} or recursive_payload:
            # Truly empty fields, and populated free-form payloads containing no
            # string descendants, keep the detector-free empty-input receipt.
            # A terminal ``**`` accepts arbitrary JSON scalar/container shapes;
            # no visited leaf therefore means "nothing authorable", not drift.
            _author_leaf("")
        elif not getattr(tenant, "layer1_placeholder_writes", False):
            # Flag-off is byte-identical even when a legacy payload does not
            # match today's registered shape; it must not emit a live error.
            _author_leaf("")
        else:
            # A populated blob that exposes no registered string leaf is not
            # clean: preserve it fail-open and leave it eligible for repair.
            shape_mismatch = _finalize(
                tenant,
                "",
                {
                    "state": "unconfirmed",
                    "reason": "shape-mismatch",
                    "redactions": [],
                },
                seam=seam,
                writer=writer,
                field=field,
                checked=True,
                live=live,
                model_label=model_label,
            )
            leaf_receipts.append(shape_mismatch.receipt)
    return AuthoredJSON(
        value=authored_value,
        receipt=_aggregate_json_receipts(leaf_receipts, writer=writer),
    )
