"""Bounded repair sweep for unconfirmed/residual placeholder writes."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When

from apps.pii.alerts import send_rate_alert
from apps.pii.authoring import author_json_paths, author_text
from apps.pii.store_registry import registered_stores

logger = logging.getLogger(__name__)

REPAIR_STATES = frozenset({"unconfirmed", "residual"})
DEFAULT_BATCH_SIZE = 200
MAX_REPAIR_ATTEMPTS = 3


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


def repair_tenant(tenant, *, max_rows: int = DEFAULT_BATCH_SIZE, alert: bool = True) -> dict[str, int]:
    """Repair up to ``max_rows`` registered rows for one flag-enabled tenant."""
    totals = {
        "rows_seen": 0,
        "fields_attempted": 0,
        "fields_repaired": 0,
        "unconfirmed": 0,
        "residual": 0,
        "terminal": 0,
        "errors": 0,
    }
    if max_rows <= 0 or not getattr(tenant, "layer1_placeholder_writes", False):
        return totals

    remaining = max_rows
    for store in registered_stores():
        if remaining <= 0:
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
            .order_by("_repair_priority", "pk")[:remaining]
        )
        for row in rows:
            totals["rows_seen"] += 1
            remaining -= 1
            receipts = dict(getattr(row, store.receipts_field, {}) or {})
            original_receipts = dict(receipts)
            changed_fields: list[str] = []
            attempted_fields: list[str] = []
            repaired_fields = 0
            row_terminal = 0
            for field in store.flat_fields:
                old_receipt = receipts.get(field)
                old_state = old_receipt.get("state") if isinstance(old_receipt, dict) else None
                if old_state not in REPAIR_STATES:
                    continue
                totals["fields_attempted"] += 1
                attempted_fields.append(field)
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
                    totals["unconfirmed"] += 1
                elif state == "residual":
                    totals["residual"] += 1
                if state in REPAIR_STATES:
                    receipt, terminal = _fruitless_receipt(old_receipt, receipt)
                    row_terminal += int(terminal)
                    state = receipt.get("state")
                receipts[field] = receipt
                if state not in {*REPAIR_STATES, "terminal"}:
                    repaired_fields += 1

            for field in store.json_fields:
                old_receipt = receipts.get(field)
                old_state = old_receipt.get("state") if isinstance(old_receipt, dict) else None
                if old_state not in REPAIR_STATES:
                    continue
                totals["fields_attempted"] += 1
                attempted_fields.append(field)
                try:
                    authored = author_json_paths(
                        tenant,
                        getattr(row, field),
                        paths=store.nested_json_paths(field),
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

                if authored.value != getattr(row, field):
                    setattr(row, field, authored.value)
                    changed_fields.append(field)
                receipt = authored.receipt
                state = receipt.get("state")
                if state == "unconfirmed":
                    totals["unconfirmed"] += 1
                elif state == "residual":
                    totals["residual"] += 1
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
            if changed_fields:
                try:
                    # Isolate a database-level write failure so the receipt-only
                    # fallback below can still run inside callers' transactions.
                    with transaction.atomic():
                        row.save(update_fields=list(dict.fromkeys(changed_fields)))
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
                        with transaction.atomic():
                            persisted = store.model.objects.filter(pk=row.pk, tenant=tenant).update(
                                **{store.receipts_field: fallback_receipts}
                            )
                    except Exception:
                        logger.exception(
                            "pii_repair_receipt_save_error tenant=%s store=%s row=%s",
                            tenant.pk,
                            store.model_label,
                            row.pk,
                        )
                    else:
                        if persisted:
                            totals["terminal"] += fallback_terminal
                        else:
                            logger.warning(
                                "pii_repair_receipt_save_missing tenant=%s store=%s row=%s",
                                tenant.pk,
                                store.model_label,
                                row.pk,
                            )
                    continue
            totals["terminal"] += row_terminal
            totals["fields_repaired"] += repaired_fields

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
        "pii_repair_counter tenant=%s rows_seen=%d fields_attempted=%d fields_repaired=%d "
        "unconfirmed=%d residual=%d terminal=%d errors=%d",
        tenant.pk,
        totals["rows_seen"],
        totals["fields_attempted"],
        totals["fields_repaired"],
        totals["unconfirmed"],
        totals["residual"],
        totals["terminal"],
        totals["errors"],
    )
    return totals


def sweep_placeholder_repairs(*, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, int]:
    """Repair a globally bounded fleet batch, with tenant-level telemetry."""
    from apps.tenants.models import Tenant

    totals = {
        "tenants_seen": 0,
        "rows_seen": 0,
        "fields_attempted": 0,
        "fields_repaired": 0,
        "unconfirmed": 0,
        "residual": 0,
        "terminal": 0,
        "errors": 0,
    }
    remaining = max(0, batch_size)
    tenants = (
        Tenant.objects.filter(
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
        .order_by("id")
    )
    for tenant in tenants:
        if remaining <= 0:
            break
        result = repair_tenant(tenant, max_rows=remaining)
        if not result["rows_seen"]:
            continue
        totals["tenants_seen"] += 1
        remaining -= result["rows_seen"]
        for field in totals:
            if field != "tenants_seen":
                totals[field] += result[field]

    logger.info(
        "pii_repair_sweep complete batch_size=%d tenants_seen=%d rows_seen=%d "
        "fields_attempted=%d fields_repaired=%d unconfirmed=%d residual=%d terminal=%d errors=%d",
        batch_size,
        totals["tenants_seen"],
        totals["rows_seen"],
        totals["fields_attempted"],
        totals["fields_repaired"],
        totals["unconfirmed"],
        totals["residual"],
        totals["terminal"],
        totals["errors"],
    )
    return totals


def placeholder_repair_sweep_task() -> dict[str, int]:
    """QStash entrypoint for the hourly, retry/DLQ-backed repair sweep."""
    return sweep_placeholder_repairs()
