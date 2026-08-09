"""Bounded W4 preflight for demoting known false-clean receipts."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.pii.historical_migration import (
    DEFAULT_BATCH_SIZE,
    _chain_dedup_id,
    _conditional_update,
    _receipt_state,
    _row_queryset,
    _row_version,
    json_field_yields_no_registered_leaves,
    normalize_batch_size,
    w4_migration_tenant_allowed,
)
from apps.pii.store_registry import PlaceholderStore, registered_store, registered_stores
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiptDemotionBatchResult:
    store_label: str
    done: bool
    skipped: bool
    rows_scanned: int
    last_pk: str
    counts: dict[str, int]


def parse_deploy_cutoff(value: str | datetime) -> datetime:
    """Require an explicit timezone-aware d24cf4b5 production deploy cutoff."""
    cutoff = value if isinstance(value, datetime) else parse_datetime(value)
    if cutoff is None or timezone.is_naive(cutoff):
        raise ValueError("deploy_cutoff must be a timezone-aware ISO-8601 datetime")
    return cutoff


def _time_discriminator_field(store: PlaceholderStore) -> str | None:
    for field in ("updated_at", "created_at"):
        try:
            store.model._meta.get_field(field)
        except FieldDoesNotExist:
            continue
        return field
    return None


def _field_demotion_reasons(
    row: Any,
    store: PlaceholderStore,
    field: str,
    cutoff: datetime,
    time_discriminator: str,
) -> tuple[str, ...]:
    receipts = getattr(row, store.receipts_field, {})
    receipt = receipts.get(field) if isinstance(receipts, dict) else None
    if not isinstance(receipt, dict) or _receipt_state(receipts, field) != "placeholder":
        return ()

    reasons: list[str] = []
    # d24cf4b5 added residual detection to both writer paths; receipts from
    # either path before its deploy cutoff are therefore equally false-clean.
    if receipt.get("writer") in {"runtime", "background"} and getattr(row, time_discriminator) < cutoff:
        reasons.append("runtime_pre_cutoff")
    if field in store.json_fields and json_field_yields_no_registered_leaves(row, store, field):
        reasons.append("no_leaf_shape")
    return tuple(reasons)


def process_receipt_demotion_batch(
    tenant: Tenant,
    store_label: str,
    deploy_cutoff: str | datetime,
    *,
    commit: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    after_pk: str = "",
) -> ReceiptDemotionBatchResult:
    """Scan one bounded PK window and demote matching field receipts only."""
    store = registered_store(store_label)
    cutoff = parse_deploy_cutoff(deploy_cutoff)
    batch_size = normalize_batch_size(batch_size)
    if not getattr(tenant, "layer1_placeholder_writes", False):
        counts = {"flag_disabled_skipped": 0}
        emit_receipt_demotion_report(tenant.pk, store.model_label, counts)
        return ReceiptDemotionBatchResult(store.model_label, True, True, 0, after_pk, counts)
    time_discriminator = _time_discriminator_field(store)
    if time_discriminator is None:
        counts = {"time_discriminator_missing_skipped": 0}
        emit_receipt_demotion_report(tenant.pk, store.model_label, counts)
        return ReceiptDemotionBatchResult(store.model_label, True, True, 0, after_pk, counts)

    queryset = _row_queryset(tenant, store).order_by("pk")
    if after_pk:
        queryset = queryset.filter(pk__gt=store.model._meta.pk.to_python(after_pk))
    rows = list(queryset[:batch_size])
    counts: Counter[str] = Counter()
    watermark = after_pk

    for row in rows:
        version = _row_version(row, store)
        receipts = dict(getattr(row, store.receipts_field, {}) or {})
        changed = False
        for field in store.receipt_fields:
            reasons = _field_demotion_reasons(row, store, field, cutoff, time_discriminator)
            if not reasons:
                continue
            counts["matched"] += 1
            for reason in reasons:
                counts[reason] += 1
            if commit:
                receipt = dict(receipts[field])
                receipt["state"] = "unconfirmed"
                receipt["reason"] = "w4-receipt-demotion"
                receipts[field] = receipt
                changed = True

        if changed:
            if _conditional_update(row, store, version, {store.receipts_field: receipts}):
                counts["demoted"] += 1
            else:
                counts["changed_skipped"] += 1
        watermark = str(row.pk)

    done = len(rows) < batch_size
    emit_receipt_demotion_report(tenant.pk, store.model_label, counts)
    return ReceiptDemotionBatchResult(
        store.model_label,
        done,
        False,
        len(rows),
        watermark,
        dict(counts),
    )


def emit_receipt_demotion_report(tenant_id: Any, store_label: str, counts: dict[str, int]) -> None:
    logger.info(
        "w4_receipt_demotion_report tenant=%s store=%s matched=%d runtime_pre_cutoff=%d "
        "no_leaf_shape=%d demoted=%d changed_skipped=%d flag_disabled_skipped=%d "
        "time_discriminator_missing_skipped=%d",
        tenant_id,
        store_label,
        counts.get("matched", 0),
        counts.get("runtime_pre_cutoff", 0),
        counts.get("no_leaf_shape", 0),
        counts.get("demoted", 0),
        counts.get("changed_skipped", 0),
        counts.get("flag_disabled_skipped", 0),
        counts.get("time_discriminator_missing_skipped", 0),
    )


def w4_receipt_demotion_task(
    tenant_id: str,
    deploy_cutoff: str,
    *,
    commit: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    store_index: int = 0,
    after_pk: str = "",
) -> dict[str, Any]:
    """QStash chain that scans one bounded receipt-demotion window per fire."""
    if type(commit) is not bool:
        raise ValueError("commit must be a JSON boolean")
    if isinstance(store_index, bool) or not isinstance(store_index, int) or store_index < 0:
        raise ValueError("store_index must be a non-negative integer")
    if not isinstance(after_pk, str):
        raise ValueError("after_pk must be a string")
    cutoff = parse_deploy_cutoff(deploy_cutoff)
    batch_size = normalize_batch_size(batch_size)
    tenant = Tenant.objects.get(pk=tenant_id)
    stores = registered_stores()

    if commit and not w4_migration_tenant_allowed(tenant):
        return {"tenant_id": str(tenant.pk), "status": "not_gated", "mode": "commit"}
    if not getattr(tenant, "layer1_placeholder_writes", False):
        emit_receipt_demotion_report(tenant.pk, "-", {"flag_disabled_skipped": 0})
        return {"tenant_id": str(tenant.pk), "status": "flag_disabled", "mode": "commit" if commit else "dry-run"}
    if store_index >= len(stores):
        return {"tenant_id": str(tenant.pk), "status": "complete", "mode": "commit" if commit else "dry-run"}

    store = stores[store_index]
    result = process_receipt_demotion_batch(
        tenant,
        store.model_label,
        cutoff,
        commit=commit,
        batch_size=batch_size,
        after_pk=after_pk,
    )
    next_store_index = store_index + 1 if result.done else store_index
    next_after_pk = "" if result.done else result.last_pk

    from apps.cron.publish import publish_task

    publish_task(
        "w4_receipt_demotion",
        str(tenant.pk),
        cutoff.isoformat(),
        commit=commit,
        batch_size=batch_size,
        store_index=next_store_index,
        after_pk=next_after_pk,
        idempotency_key=_chain_dedup_id(
            "w4_receipt_demotion",
            tenant.pk,
            "commit" if commit else "dry-run",
            cutoff.isoformat(),
            next_store_index,
            next_after_pk,
        ),
    )
    return {
        "tenant_id": str(tenant.pk),
        "status": "chained",
        "mode": "commit" if commit else "dry-run",
        "store": store.model_label,
        "rows_scanned": result.rows_scanned,
        "last_pk": result.last_pk,
    }
