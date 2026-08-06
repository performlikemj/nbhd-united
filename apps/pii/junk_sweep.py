"""Zero-egress, deterministic self-cleaning sweep over historical PII bindings.

The prod audit found 979/1103 canary bindings were junk (agent-authored
markdown, raw tool payloads, unvalidated neural financial labels, date/number
mislabels). The cloud arbiter (:mod:`apps.pii.arbiter`) used to prune the
PERSON/LOCATION long tail by shipping span text to Claude Haiku — that egress
is being retired. This sweep replaces it with the local deterministic hygiene
layer (:mod:`apps.pii.hygiene`); the residual ambiguous cases go to an
on-device user-review flow instead of a cloud LLM.

Heal → deny → delete, strictly in that order (WHY the order is load-bearing)
--------------------------------------------------------------------------
A binding maps a placeholder ``[TYPE_N]`` to the real value it stands in for.
Owner-facing journal text stores the *placeholder*, and the rehydration seam
swaps it back to the real value at read time. So the order is a safety
sequence, not a preference:

  1. HEAL first. Rewrite the placeholder tokens still sitting in stored
     owner-visible text to their bound value BEFORE we drop the binding — once
     the ``pii_entity_map`` entry is gone, rehydration can no longer resolve
     ``[TYPE_N]`` and the owner would see a raw placeholder forever. Only exact
     ``[TYPE_N]`` / ``\\[TYPE_N\\]`` tokens are replaced — never the raw value —
     so a junk value that is a common word ("django", "2026-05-30") appearing
     naturally in prose is left untouched.
  2. DENY next. Add the canonical key to ``pii_denylist`` so the redactor's
     detection pass can't re-mint the same junk on the next inbound message.
     If we deleted before denying, the very next message would coin a fresh
     ``[TYPE_N]`` for the same span and we'd be back where we started.
  3. DELETE last. Drop the binding from ``pii_entity_map``.

All three run under the tenant ``select_for_update`` lock (mirroring the
entity-registry mutations in ``apps/tenants/views.py``): the inbound redactor
and the settings UI both overwrite the whole JSON dicts, so we re-read the
locked row and write both fields in a single ``.update()`` to avoid clobbering
— or being clobbered by — a concurrent mint/delete.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.pii.entity_registry import canonical_key, get_name
from apps.pii.hygiene import is_junk_span, validate_structured

logger = logging.getLogger(__name__)

# Parse ``[TYPE_N]`` → (TYPE, N). TYPE allows underscores (EMAIL_ADDRESS,
# CREDIT_CARD, IP_ADDRESS); the trailing ``_N`` is the mint counter. Anchored so
# a malformed placeholder yields an empty type (→ is_junk_span alone, no
# structured validation).
_PLACEHOLDER_RE = re.compile(r"^\[([A-Z_]+)_(\d+)\]$")

# Types :func:`validate_structured` gates with a shape/checksum. PERSON/LOCATION
# are excluded ON PURPOSE — validate_structured returns False for them by
# contract (they have no structural shape), so running a real name through it
# would false-junk every keeper. Unknown/other placeholder types fall back to
# is_junk_span alone (conservative: false-junk on real PII is the failure mode
# to avoid). Mirrors the branches inside apps/pii/hygiene.validate_structured.
_VALIDATED_STRUCTURED_TYPES = frozenset(
    {
        "EMAIL_ADDRESS",
        "CREDIT_CARD",
        "IBAN_CODE",
        "PHONE_NUMBER",
        "IP_ADDRESS",
        "PASSWORD",
        "ACCOUNT",
        "CRYPTO_ADDRESS",
        "ID_DOCUMENT",
    }
)

DEFAULT_MAX_ENTRIES = 500


def _entity_type(placeholder: str) -> str:
    """Return the TYPE part of ``[TYPE_N]``, or ``""`` for a malformed token."""
    match = _PLACEHOLDER_RE.match(placeholder)
    return match.group(1) if match else ""


def classify_entry(placeholder: str, name: str) -> tuple[str, str]:
    """Classify a stored binding as ``('junk', reason)`` or ``('keep', '')``.

    Zero-egress: :func:`is_junk_span` (structure / invisible-char / placeholder-
    fragment / date-number rules) plus :func:`validate_structured` (Luhn / IBAN
    / email / secret-run checks) — no network, no LLM. The entity type is
    derived from the placeholder prefix so the exact gate the redactor applies
    at mint time reruns over the historical binding.
    """
    entity_type = _entity_type(placeholder)
    junk, reason = is_junk_span(name, entity_type)
    if junk:
        return "junk", reason
    # Structured types with no valid shape/checksum are junk (the audit's
    # "django"/"USER.md" as CREDIT_CARD, temperature-range ACCOUNT). PERSON/
    # LOCATION and unknown types are never gated here — they'd all fail.
    if entity_type in _VALIDATED_STRUCTURED_TYPES and not validate_structured(entity_type, name):
        return "junk", f"invalid_{entity_type.lower()}"
    return "keep", ""


def _denyable(key: str) -> bool:
    """True when a canonical key is worth adding to the denylist.

    Skips keys shorter than 2 chars or carrying no letter — a bare number or
    single char makes a uselessly broad denylist entry that could suppress
    legitimate future spans, and the binding is deleted regardless.
    """
    return len(key) >= 2 and any(ch.isalpha() for ch in key)


def _classify(entity_map: dict[str, Any], max_entries: int) -> tuple[dict[str, int], dict[str, dict[str, str]]]:
    """Return ``(summary_counts, junk)`` for up to ``max_entries`` bindings.

    ``junk`` maps ``placeholder -> {"name", "reason", "key"}``. The slice is
    deterministic (dict insertion order) so a run and its dry-run agree, and a
    huge legacy map can't blow the per-run budget.
    """
    summary = {"examined": 0, "junk": 0, "healed_rows": 0, "denied": 0, "deleted": 0, "skipped": 0}
    junk: dict[str, dict[str, str]] = {}
    for placeholder, entry in list(entity_map.items())[:max_entries]:
        summary["examined"] += 1
        name = get_name(entry)
        verdict, reason = classify_entry(placeholder, name)
        if verdict == "junk":
            summary["junk"] += 1
            junk[placeholder] = {"name": name, "reason": reason, "key": canonical_key(name)}
        else:
            summary["skipped"] += 1
    return summary, junk


def _build_heal_regex(inners: list[str]) -> re.Pattern[str] | None:
    """Compile a matcher for the plain and markdown-escaped forms of the given
    placeholder inners (``"PERSON_5"`` → matches ``[PERSON_5]`` and
    ``\\[PERSON_5\\]``). Returns None when there is nothing to match.

    Matching bracketed tokens ONLY is the guardrail against rewriting raw
    value text: the value never appears here, only ``\\?\\[<inner>\\?\\]``.
    """
    if not inners:
        return None
    alternation = "|".join(re.escape(inner) for inner in inners)
    return re.compile(r"\\?\[(" + alternation + r")\\?\]")


def _heal_rows(tenant: Any, ph_to_name: dict[str, str]) -> int:
    """Replace junk placeholder tokens with their bound value across the
    tenant's owner-visible journal storage. Returns the number of rows changed.

    Only exact ``[TYPE_N]`` / ``\\[TYPE_N\\]`` tokens are substituted — never the
    raw value — so a junk value that also occurs as ordinary prose is untouched.
    """
    from apps.journal.models import Document, DocumentChunk
    from apps.pii.store_registry import registered_stores

    inner_to_name: dict[str, str] = {}
    for placeholder, name in ph_to_name.items():
        match = _PLACEHOLDER_RE.match(placeholder)
        if match:
            inner_to_name[f"{match.group(1)}_{match.group(2)}"] = name or ""

    regex = _build_heal_regex(list(inner_to_name))
    if regex is None:
        return 0

    def _sub(value: str) -> str:
        # Every placeholder — plain or escaped — carries a '['; skip rows
        # without one so a bracketless value short-circuits before regex work.
        if not value or "[" not in value:
            return value
        return regex.sub(lambda m: inner_to_name.get(m.group(1), m.group(0)), value)

    healed = 0

    # (model, [text fields], receipts field) — each row is counted once no matter how many of
    # its fields changed. ``__contains="["`` narrows to rows that could hold a
    # token ('[' is literal in Postgres LIKE).
    text_targets = [
        (Document, ("markdown", "title"), None),
        (DocumentChunk, ("text",), None),
    ]
    for store in registered_stores():
        if store.json_paths:
            # TODO(P3/W2): add receipt-aware JSON-path healing before registering
            # the first nested placeholder-bearing surface.
            raise NotImplementedError(f"JSON-path healing is not implemented for {store.model_label}")
        text_targets.append((store.model, store.flat_fields, store.receipts_field))

    for model, fields, receipts_field in text_targets:
        bracket_q = Q()
        for field in fields:
            bracket_q |= Q(**{f"{field}__contains": "["})
        only_fields = ["id", *fields]
        if receipts_field:
            only_fields.append(receipts_field)
        rows = model.objects.filter(tenant=tenant).filter(bracket_q).only(*only_fields)
        for row in rows:
            changed_fields = []
            receipts = dict(getattr(row, receipts_field, {}) or {}) if receipts_field else {}
            for field in fields:
                current = getattr(row, field)
                healed_value = _sub(current)
                if healed_value != current:
                    setattr(row, field, healed_value)
                    changed_fields.append(field)
                    receipt = receipts.get(field)
                    if isinstance(receipt, dict) and isinstance(receipt.get("redactions"), list):
                        next_receipt = dict(receipt)
                        next_receipt["redactions"] = [
                            item
                            for item in receipt["redactions"]
                            if not isinstance(item, dict) or item.get("placeholder") not in ph_to_name
                        ]
                        receipts[field] = next_receipt
            if receipts_field and receipts != (getattr(row, receipts_field, {}) or {}):
                setattr(row, receipts_field, receipts)
                changed_fields.append(receipts_field)
            if changed_fields:
                # Deliberately omit auto_now ``updated_at`` — a hygiene rewrite
                # isn't a user edit and shouldn't reorder the owner's timeline.
                row.save(update_fields=changed_fields)
                healed += 1

    return healed


def sweep_tenant(tenant: Any, *, dry_run: bool = False, max_entries: int = DEFAULT_MAX_ENTRIES) -> dict[str, int]:
    """Heal → deny → delete every deterministic-junk binding for one tenant.

    Returns ``{examined, junk, healed_rows, denied, deleted, skipped}``. On
    ``dry_run`` it classifies and reports counts without touching anything.

    The real path classifies UNDER the row lock (from the freshly re-read map)
    so there is no stale-read window: the junk set that drives heal/deny/delete
    is authoritative for the row we mutate.
    """
    from apps.tenants.models import Tenant

    if dry_run:
        summary, _ = _classify(getattr(tenant, "pii_entity_map", None) or {}, max_entries)
        return summary

    with transaction.atomic():
        locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
        if locked is None:
            return {"examined": 0, "junk": 0, "healed_rows": 0, "denied": 0, "deleted": 0, "skipped": 0}

        entity_map = dict(locked.pii_entity_map or {})
        denylist = dict(locked.pii_denylist or {})

        summary, junk = _classify(entity_map, max_entries)
        if not junk:
            return summary

        # (a) HEAL owner-visible text first — placeholder tokens only.
        ph_to_name = {placeholder: meta["name"] for placeholder, meta in junk.items()}
        summary["healed_rows"] = _heal_rows(tenant, ph_to_name)

        # (b) DENY canonical keys so the redactor can't re-mint the same junk.
        now_iso = timezone.now().isoformat()
        denied_keys: set[str] = set()
        for meta in junk.values():
            key = meta["key"]
            if not _denyable(key) or key in denylist or key in denied_keys:
                continue
            denylist[key] = {"reason": f"junk-sweep:{meta['reason']}", "decided_at": now_iso}
            denied_keys.add(key)
        summary["denied"] = len(denied_keys)

        # (c) DELETE the bindings.
        for placeholder in junk:
            if entity_map.pop(placeholder, None) is not None:
                summary["deleted"] += 1

        Tenant.objects.filter(pk=tenant.pk).update(pii_entity_map=entity_map, pii_denylist=denylist)
        locked.pii_entity_map = entity_map
        locked.pii_denylist = denylist

    # Keep the caller's in-memory tenant consistent with what we wrote.
    tenant.pii_entity_map = entity_map
    tenant.pii_denylist = denylist
    return summary


def sweep_all_tenants(
    *, dry_run: bool = False, max_entries: int = DEFAULT_MAX_ENTRIES, max_tenants: int | None = None
) -> dict[str, int]:
    """Sweep every active tenant that has any bindings, with per-tenant error
    isolation — one tenant's failure is logged and the sweep continues.

    Mirrors ``pii_arbiter_task``'s iteration (only-fields projection, empty-map
    exclusion, ordered by id). Returns fleet totals for the cron log.
    """
    from apps.tenants.models import Tenant

    totals = {
        "tenants_seen": 0,
        "tenants_with_junk": 0,
        "examined": 0,
        "junk": 0,
        "healed_rows": 0,
        "denied": 0,
        "deleted": 0,
        "skipped": 0,
        "errors": 0,
    }

    candidate_tenants = (
        Tenant.objects.filter(status=Tenant.Status.ACTIVE)
        .exclude(pii_entity_map={})
        .only("id", "pii_entity_map", "pii_denylist")
        .order_by("id")
    )
    if max_tenants is not None:
        candidate_tenants = candidate_tenants[:max_tenants]

    for tenant in candidate_tenants:
        totals["tenants_seen"] += 1
        try:
            result = sweep_tenant(tenant, dry_run=dry_run, max_entries=max_entries)
        except Exception:
            totals["errors"] += 1
            logger.exception("pii_junk_sweep failed for tenant=%s", tenant.pk)
            continue
        for field in ("examined", "junk", "healed_rows", "denied", "deleted", "skipped"):
            totals[field] += result[field]
        if result["junk"]:
            totals["tenants_with_junk"] += 1

    logger.info(
        "pii_junk_sweep complete tenants_seen=%d tenants_with_junk=%d examined=%d junk=%d "
        "healed_rows=%d denied=%d deleted=%d errors=%d",
        totals["tenants_seen"],
        totals["tenants_with_junk"],
        totals["examined"],
        totals["junk"],
        totals["healed_rows"],
        totals["denied"],
        totals["deleted"],
        totals["errors"],
    )
    return totals


def pii_junk_sweep_task() -> dict[str, int]:
    """QStash entrypoint: daily zero-egress hygiene sweep over the fleet.

    Replaces the retired cloud PII arbiter (``pii_arbiter_task``). The trigger
    view sets the service-role RLS context before calling, so the per-tenant
    journal queries in :func:`_heal_rows` reach every tenant's rows.
    """
    return sweep_all_tenants()
