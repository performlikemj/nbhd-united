"""Bounded repair sweep for unconfirmed/residual placeholder writes."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from itertools import chain, islice
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from apps.pii.alerts import send_rate_alert
from apps.pii.authoring import _aggregate_json_receipts, author_json_paths, author_text
from apps.pii.store_registry import PlaceholderStore, registered_stores, rewrite_json_path

logger = logging.getLogger(__name__)

REPAIR_STATES = frozenset({"unconfirmed", "residual"})
DEFAULT_BATCH_SIZE = 16
DEFAULT_TEXT_BUDGET = 16
DEFAULT_TENANT_BATCH_SIZE = 4
DEFAULT_TENANT_TEXT_BUDGET = 4
MAX_DETECTOR_CALLS_PER_TEXT = 2
MAX_DETECTOR_CALLS_PER_SWEEP = DEFAULT_TEXT_BUDGET * MAX_DETECTOR_CALLS_PER_TEXT
MAX_REPAIR_ATTEMPTS = 3
_PARTIAL_JSON_REASON = "repair-batch-partial"
_PARTIAL_JSON_PROGRESS = "repair_progress"


@dataclass(frozen=True)
class _JSONRepairChunk:
    value: Any
    receipt: dict[str, Any] | None
    texts_authored: int
    next_cursor: int
    complete: bool


@dataclass
class _DetectorWorkBudget:
    """Hard cap on strings handed to checked background authoring."""

    limit: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def reserve_text(self) -> bool:
        if self.remaining <= 0:
            return False
        self.used += 1
        return True


def _empty_totals() -> dict[str, int]:
    return {
        "rows_seen": 0,
        "fields_attempted": 0,
        "texts_authored": 0,
        "fields_repaired": 0,
        "unconfirmed": 0,
        "residual": 0,
        "terminal": 0,
        "conflicts": 0,
        "errors": 0,
    }


def _json_digest(value: Any) -> str:
    """Return a keyed metadata-only digest for validating a persisted leaf cursor."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        encoded.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _json_progress(old_receipt: Any, value: Any) -> tuple[int, dict[str, Any] | None]:
    """Resume only a cursor that belongs to the exact partially-authored value."""
    if not isinstance(old_receipt, dict) or old_receipt.get("reason") != _PARTIAL_JSON_REASON:
        return 0, None
    progress = old_receipt.get(_PARTIAL_JSON_PROGRESS)
    if not isinstance(progress, dict) or progress.get("source_digest") != _json_digest(value):
        return 0, None
    cursor = progress.get("cursor")
    aggregate = progress.get("aggregate")
    if not isinstance(cursor, int) or cursor < 0 or not isinstance(aggregate, dict):
        return 0, None
    return cursor, aggregate


def _merge_json_receipt(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    receipts = [receipt for receipt in (previous, current) if isinstance(receipt, dict)]
    return _aggregate_json_receipts(receipts, writer="background")


def _partial_json_receipt(
    old_receipt: Any,
    aggregate: dict[str, Any],
    *,
    cursor: int,
    value: Any,
) -> dict[str, Any]:
    """Persist detector-free progress without declaring an incomplete field clean."""
    receipt = {
        "state": "unconfirmed",
        "reason": _PARTIAL_JSON_REASON,
        "writer": "background",
        "redactions": list(aggregate.get("redactions", [])),
        _PARTIAL_JSON_PROGRESS: {
            "cursor": cursor,
            "source_digest": _json_digest(value),
            "aggregate": aggregate,
        },
    }
    attempts = old_receipt.get("repair_attempts") if isinstance(old_receipt, dict) else None
    if isinstance(attempts, int) and attempts >= 0:
        receipt["repair_attempts"] = attempts
    return receipt


def _author_json_chunk(
    tenant,
    value: Any,
    *,
    paths: tuple[tuple[str, ...], ...],
    seam: str,
    field: str,
    model_label: str,
    cursor: int,
    budget: _DetectorWorkBudget,
) -> _JSONRepairChunk:
    """Author a deterministic JSON leaf window without exceeding ``budget``."""
    leaf_receipts: list[dict[str, Any]] = []
    leaves_seen = 0
    texts_authored = 0

    def _author_leaf(text: str) -> str:
        nonlocal leaves_seen, texts_authored
        leaf_index = leaves_seen
        leaves_seen += 1
        if leaf_index < cursor or not budget.reserve_text():
            return text
        authored = author_text(
            tenant,
            text,
            seam=seam,
            writer="background",
            field=field,
            live=False,
            model_label=model_label,
        )
        texts_authored += 1
        leaf_receipts.append(authored.receipt)
        return authored.text

    authored_value = value
    for path in paths:
        authored_value, _changed = rewrite_json_path(authored_value, path, _author_leaf)

    if leaves_seen == 0:
        # Preserve the existing empty-input and shape-mismatch semantics. This
        # branch has no selected string leaves, so it cannot fan out detection.
        authored = author_json_paths(
            tenant,
            value,
            paths=paths,
            seam=seam,
            writer="background",
            field=field,
            live=False,
            model_label=model_label,
        )
        return _JSONRepairChunk(
            value=authored.value,
            receipt=authored.receipt,
            texts_authored=0,
            next_cursor=0,
            complete=True,
        )

    if not leaf_receipts:
        return _JSONRepairChunk(
            value=value,
            receipt=None,
            texts_authored=0,
            next_cursor=cursor,
            complete=cursor >= leaves_seen,
        )

    next_cursor = cursor + texts_authored
    return _JSONRepairChunk(
        value=authored_value,
        receipt=_aggregate_json_receipts(leaf_receipts, writer="background"),
        texts_authored=texts_authored,
        next_cursor=next_cursor,
        complete=next_cursor >= leaves_seen,
    )


def _source_snapshot(row, store: PlaceholderStore) -> dict[str, Any]:
    fields = (*store.flat_fields, *store.json_fields, store.receipts_field)
    return {field: copy.deepcopy(getattr(row, field)) for field in fields}


def _source_matches(row, snapshot: dict[str, Any]) -> bool:
    return all(getattr(row, field) == value for field, value in snapshot.items())


def _save_if_unchanged(
    row,
    store: PlaceholderStore,
    snapshot: dict[str, Any],
    *,
    changed_fields: list[str],
) -> bool:
    """Lock only after NER and apply results iff every registered source is unchanged."""
    with transaction.atomic():
        current = (
            store.model.objects.select_for_update().filter(pk=row.pk, tenant_id=row.tenant_id).only(*snapshot).first()
        )
        if current is None or not _source_matches(current, snapshot):
            return False
        unique_fields = list(dict.fromkeys(changed_fields))
        for field in unique_fields:
            setattr(current, field, getattr(row, field))
        if unique_fields:
            current.save(update_fields=unique_fields)
    return True


def _fallback_receipt_if_unchanged(
    row,
    store: PlaceholderStore,
    snapshot: dict[str, Any],
    fallback_receipts: dict[str, Any],
) -> bool:
    """Persist a failure receipt only while the pre-NER row version still matches."""
    with transaction.atomic():
        current = (
            store.model.objects.select_for_update().filter(pk=row.pk, tenant_id=row.tenant_id).only(*snapshot).first()
        )
        if current is None or not _source_matches(current, snapshot):
            return False
        store.model.objects.filter(pk=current.pk, tenant_id=row.tenant_id).update(
            **{store.receipts_field: fallback_receipts}
        )
    return True


def _rotated_stores(offset: int) -> tuple[PlaceholderStore, ...]:
    stores = registered_stores()
    if not stores:
        return ()
    pivot = offset % len(stores)
    return (*stores[pivot:], *stores[:pivot])


def _fruitless_receipt(old_receipt, candidate=None) -> tuple[dict, bool]:
    """Stamp one unsuccessful repair and terminalize after a bounded count."""
    old = old_receipt if isinstance(old_receipt, dict) else {}
    receipt = dict(candidate if isinstance(candidate, dict) else old)
    attempts = old.get("repair_attempts", 0)
    attempts = attempts if isinstance(attempts, int) and attempts >= 0 else 0
    attempts += 1
    receipt["repair_attempts"] = attempts
    if attempts < MAX_REPAIR_ATTEMPTS:
        return receipt, False
    prior_state = receipt.get("state")
    receipt.update(
        {
            "state": "terminal",
            "terminal_from": prior_state,
            "terminal_reason": "repair-attempts-exhausted",
        }
    )
    return receipt, True


def _repair_query(fields: tuple[str, ...], receipts_field: str) -> Q:
    query = Q()
    for field in fields:
        query |= Q(**{f"{receipts_field}__{field}__state__in": sorted(REPAIR_STATES)})
    return query


def _receipt_state_query(fields: tuple[str, ...], receipts_field: str, state: str) -> Q:
    query = Q()
    for field in fields:
        query |= Q(**{f"{receipts_field}__{field}__state": state})
    return query


def _check_rate_alert(tenant, *, attempts: int, count: int, kind: str) -> bool:
    """Emit a metadata-only >1% alert through the transcript alert gate."""
    return send_rate_alert(
        tenant,
        attempts=attempts,
        count=count,
        kind=kind,
        fingerprint_scope=None,
        window="current bounded repair sweep",
        counters=(("Attempts", attempts), (f"{kind.title()} outcomes", count)),
    )


def repair_tenant(
    tenant,
    *,
    max_rows: int = DEFAULT_BATCH_SIZE,
    max_texts: int = DEFAULT_TEXT_BUDGET,
    alert: bool = True,
    store_offset: int = 0,
) -> dict[str, int]:
    """Repair one tenant under both row and checked-text work budgets.

    Slow detector work happens before a short row lock. The authored result is
    saved only if every registered source field and its receipt still match the
    pre-detector snapshot, so a concurrent request write always wins.
    """
    totals = _empty_totals()
    budget = _DetectorWorkBudget(limit=min(DEFAULT_TEXT_BUDGET, max(0, max_texts)))
    if max_rows <= 0 or budget.remaining <= 0 or not getattr(tenant, "layer1_placeholder_writes", False):
        return totals

    remaining_rows = min(DEFAULT_BATCH_SIZE, max_rows)
    for store in _rotated_stores(store_offset):
        if remaining_rows <= 0 or budget.remaining <= 0:
            break
        rows = (
            store.model.objects.filter(tenant=tenant)
            .filter(_repair_query(store.receipt_fields, store.receipts_field))
            .annotate(
                _repair_priority=Case(
                    When(
                        _receipt_state_query(store.receipt_fields, store.receipts_field, "unconfirmed"),
                        then=Value(0),
                    ),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
            .order_by("_repair_priority", "pk")[:remaining_rows]
        )
        for row in rows:
            if remaining_rows <= 0 or budget.remaining <= 0:
                break
            totals["rows_seen"] += 1
            remaining_rows -= 1
            snapshot = _source_snapshot(row, store)
            receipts = copy.deepcopy(getattr(row, store.receipts_field, {}) or {})
            original_receipts = copy.deepcopy(receipts)
            changed_fields: list[str] = []
            attempted_fields: list[str] = []
            repaired_fields = 0
            row_terminal = 0
            row_unconfirmed = 0
            row_residual = 0
            for field in store.flat_fields:
                if budget.remaining <= 0:
                    break
                old_receipt = receipts.get(field)
                old_state = old_receipt.get("state") if isinstance(old_receipt, dict) else None
                if old_state not in REPAIR_STATES:
                    continue
                totals["fields_attempted"] += 1
                attempted_fields.append(field)
                budget.reserve_text()
                try:
                    authored = author_text(
                        tenant,
                        getattr(row, field),
                        seam=f"pii.repair.{store.model_label}.{field}",
                        writer="background",
                        field=field,
                        live=False,
                        model_label=store.model_label,
                    )
                except Exception:
                    totals["errors"] += 1
                    receipts[field], terminal = _fruitless_receipt(old_receipt)
                    row_terminal += int(terminal)
                    logger.exception(
                        "pii_repair_field_error tenant=%s store=%s row=%s field=%s",
                        tenant.pk,
                        store.model_label,
                        row.pk,
                        field,
                    )
                    continue

                if authored.text != getattr(row, field):
                    setattr(row, field, authored.text)
                    changed_fields.append(field)
                receipt = authored.receipt
                state = receipt.get("state")
                if state == "unconfirmed":
                    row_unconfirmed += 1
                elif state == "residual":
                    row_residual += 1
                if state in REPAIR_STATES:
                    receipt, terminal = _fruitless_receipt(old_receipt, receipt)
                    row_terminal += int(terminal)
                    state = receipt.get("state")
                receipts[field] = receipt
                if state not in {*REPAIR_STATES, "terminal"}:
                    repaired_fields += 1

            for field in store.json_fields:
                if budget.remaining <= 0:
                    break
                old_receipt = receipts.get(field)
                old_state = old_receipt.get("state") if isinstance(old_receipt, dict) else None
                if old_state not in REPAIR_STATES:
                    continue
                totals["fields_attempted"] += 1
                attempted_fields.append(field)
                source_value = getattr(row, field)
                cursor, previous_aggregate = _json_progress(old_receipt, source_value)
                try:
                    chunk = _author_json_chunk(
                        tenant,
                        source_value,
                        paths=store.nested_json_paths(field),
                        seam=f"pii.repair.{store.model_label}.{field}",
                        field=field,
                        model_label=store.model_label,
                        cursor=cursor,
                        budget=budget,
                    )
                except Exception:
                    totals["errors"] += 1
                    receipts[field], terminal = _fruitless_receipt(old_receipt)
                    row_terminal += int(terminal)
                    logger.exception(
                        "pii_repair_field_error tenant=%s store=%s row=%s field=%s",
                        tenant.pk,
                        store.model_label,
                        row.pk,
                        field,
                    )
                    continue

                if chunk.value != source_value:
                    setattr(row, field, chunk.value)
                    changed_fields.append(field)
                if not chunk.complete:
                    if chunk.receipt is None:
                        continue
                    aggregate = _merge_json_receipt(previous_aggregate, chunk.receipt)
                    receipts[field] = _partial_json_receipt(
                        old_receipt,
                        aggregate,
                        cursor=chunk.next_cursor,
                        value=chunk.value,
                    )
                    continue

                if chunk.receipt is not None:
                    receipt = _merge_json_receipt(previous_aggregate, chunk.receipt)
                elif previous_aggregate is not None:
                    receipt = previous_aggregate
                else:
                    # The zero-leaf branch always supplies a shape/empty receipt;
                    # this is a defensive retry state for corrupt progress only.
                    receipt = {
                        "state": "unconfirmed",
                        "reason": "repair-progress-invalid",
                        "redactions": [],
                        "writer": "background",
                    }
                state = receipt.get("state")
                if state == "unconfirmed":
                    row_unconfirmed += 1
                elif state == "residual":
                    row_residual += 1
                if state in REPAIR_STATES:
                    receipt, terminal = _fruitless_receipt(old_receipt, receipt)
                    row_terminal += int(terminal)
                    state = receipt.get("state")
                receipts[field] = receipt
                if state not in {*REPAIR_STATES, "terminal"}:
                    repaired_fields += 1

            if receipts != (getattr(row, store.receipts_field, {}) or {}):
                setattr(row, store.receipts_field, receipts)
                changed_fields.append(store.receipts_field)
            if attempted_fields:
                try:
                    persisted = _save_if_unchanged(
                        row,
                        store,
                        snapshot,
                        changed_fields=changed_fields,
                    )
                except Exception:
                    totals["errors"] += 1
                    logger.exception(
                        "pii_repair_row_save_error tenant=%s store=%s row=%s fields=%s",
                        tenant.pk,
                        store.model_label,
                        row.pk,
                        ",".join(dict.fromkeys(changed_fields)),
                    )
                    # The authored column did not persist, so its success
                    # receipt must not persist either. Stamp each attempted
                    # ORIGINAL receipt as fruitless and save only the receipt
                    # column, bypassing a model ``save``/field value that may be
                    # the source of the DataError. Tenant + PK scoping prevents
                    # this recovery path from crossing ownership boundaries.
                    fallback_receipts = dict(original_receipts)
                    fallback_terminal = 0
                    for attempted_field in attempted_fields:
                        fallback_receipts[attempted_field], terminal = _fruitless_receipt(
                            original_receipts.get(attempted_field)
                        )
                        fallback_terminal += int(terminal)
                    try:
                        fallback_persisted = _fallback_receipt_if_unchanged(
                            row,
                            store,
                            snapshot,
                            fallback_receipts,
                        )
                    except Exception:
                        logger.exception(
                            "pii_repair_receipt_save_error tenant=%s store=%s row=%s",
                            tenant.pk,
                            store.model_label,
                            row.pk,
                        )
                    else:
                        if fallback_persisted:
                            totals["terminal"] += fallback_terminal
                        else:
                            totals["conflicts"] += 1
                            logger.warning(
                                "pii_repair_receipt_save_conflict tenant=%s store=%s row=%s",
                                tenant.pk,
                                store.model_label,
                                row.pk,
                            )
                    continue
                if not persisted:
                    totals["conflicts"] += 1
                    logger.info(
                        "pii_repair_row_conflict tenant=%s store=%s row=%s",
                        tenant.pk,
                        store.model_label,
                        row.pk,
                    )
                    continue
            totals["terminal"] += row_terminal
            totals["fields_repaired"] += repaired_fields
            totals["unconfirmed"] += row_unconfirmed
            totals["residual"] += row_residual

    totals["texts_authored"] = budget.used

    if alert:
        _check_rate_alert(
            tenant,
            attempts=totals["fields_attempted"],
            count=totals["unconfirmed"] + totals["errors"],
            kind="error",
        )
        _check_rate_alert(
            tenant,
            attempts=totals["fields_attempted"],
            count=totals["residual"],
            kind="residual",
        )
        # Terminal outcomes have their own >1% threshold/fingerprint so an
        # exhausted repair population is alarm-visible rather than blending
        # into the transient error or residual rates.
        _check_rate_alert(
            tenant,
            attempts=totals["fields_attempted"],
            count=totals["terminal"],
            kind="terminal",
        )
    logger.info(
        "pii_repair_counter tenant=%s rows_seen=%d fields_attempted=%d texts_authored=%d "
        "fields_repaired=%d unconfirmed=%d residual=%d terminal=%d conflicts=%d errors=%d",
        tenant.pk,
        totals["rows_seen"],
        totals["fields_attempted"],
        totals["texts_authored"],
        totals["fields_repaired"],
        totals["unconfirmed"],
        totals["residual"],
        totals["terminal"],
        totals["conflicts"],
        totals["errors"],
    )
    return totals


def sweep_placeholder_repairs(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    text_budget: int = DEFAULT_TEXT_BUDGET,
    tenant_batch_size: int = DEFAULT_TENANT_BATCH_SIZE,
    tenant_text_budget: int = DEFAULT_TENANT_TEXT_BUDGET,
    fairness_tick: int | None = None,
) -> dict[str, int]:
    """Repair a bounded, rotating fleet window with tenant-level telemetry."""
    from apps.tenants.models import Tenant

    totals = {"tenants_seen": 0, **_empty_totals()}
    remaining_rows = min(DEFAULT_BATCH_SIZE, max(0, batch_size))
    effective_text_budget = min(DEFAULT_TEXT_BUDGET, max(0, text_budget))
    remaining_texts = effective_text_budget
    if remaining_rows <= 0 or remaining_texts <= 0 or tenant_batch_size <= 0 or tenant_text_budget <= 0:
        return totals
    per_tenant_rows = min(DEFAULT_TENANT_BATCH_SIZE, tenant_batch_size)
    per_tenant_texts = min(DEFAULT_TENANT_TEXT_BUDGET, tenant_text_budget)
    tenant_ids = (
        Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=True,
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    tenant_count = tenant_ids.count()
    if not tenant_count:
        return totals

    tick = fairness_tick
    if tick is None:
        tick = int(timezone.now().timestamp() // 3600)
    expected_tenant_slots = max(1, (remaining_texts + per_tenant_texts - 1) // per_tenant_texts)
    pivot = (tick * expected_tenant_slots) % tenant_count
    rotated_tenant_ids = chain(
        tenant_ids[pivot:].iterator(chunk_size=DEFAULT_BATCH_SIZE),
        tenant_ids[:pivot].iterator(chunk_size=DEFAULT_BATCH_SIZE),
    )
    fleet_cycle_hours = max(1, (tenant_count + expected_tenant_slots - 1) // expected_tenant_slots)
    store_round = tick // fleet_cycle_hours

    for tenant_index, tenant_id in enumerate(islice(rotated_tenant_ids, expected_tenant_slots)):
        if remaining_rows <= 0 or remaining_texts <= 0:
            break
        tenant = (
            Tenant.objects.filter(
                pk=tenant_id,
                status=Tenant.Status.ACTIVE,
                layer1_placeholder_writes=True,
            )
            .only(
                "id",
                "layer1_placeholder_writes",
                "model_tier",
                "pii_entity_map",
                "pii_denylist",
                "pii_type_counters",
            )
            .first()
        )
        if tenant is None:
            continue
        result = repair_tenant(
            tenant,
            max_rows=min(remaining_rows, per_tenant_rows),
            max_texts=min(remaining_texts, per_tenant_texts),
            store_offset=store_round + tenant_index,
        )
        if not result["rows_seen"]:
            continue
        totals["tenants_seen"] += 1
        remaining_rows -= result["rows_seen"]
        remaining_texts -= result["texts_authored"]
        for field in totals:
            if field != "tenants_seen":
                totals[field] += result[field]

    logger.info(
        "pii_repair_sweep complete batch_size=%d text_budget=%d max_detector_calls=%d "
        "tenants_seen=%d rows_seen=%d fields_attempted=%d texts_authored=%d fields_repaired=%d "
        "unconfirmed=%d residual=%d terminal=%d conflicts=%d errors=%d",
        batch_size,
        effective_text_budget,
        effective_text_budget * MAX_DETECTOR_CALLS_PER_TEXT,
        totals["tenants_seen"],
        totals["rows_seen"],
        totals["fields_attempted"],
        totals["texts_authored"],
        totals["fields_repaired"],
        totals["unconfirmed"],
        totals["residual"],
        totals["terminal"],
        totals["conflicts"],
        totals["errors"],
    )
    return totals


def placeholder_repair_sweep_task() -> dict[str, int]:
    """QStash entrypoint for the hourly, retry/DLQ-backed repair sweep."""
    return sweep_placeholder_repairs()
