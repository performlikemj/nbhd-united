"""Dry-run-first W4 migration of registered Layer-1 history.

The unit of work is one bounded ``(tenant, store)`` primary-key window.  A
batch detects first, persists every new tenant binding under one tenant-row
lock, then re-reads and conditionally rewrites rows after that lock is gone.
Only aggregate counts are logged; content and detected values never are.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import connection, transaction
from django.db.models.expressions import RawSQL
from django.utils import timezone

from apps.pii.authoring import author_json_paths, author_text
from apps.pii.entity_registry import canonical_key, get_name, inverted_names_ci, to_storage_value
from apps.pii.redactor import MINT_ALL, MINT_NEVER, RedactionSession, next_placeholder_number
from apps.pii.repair_sweep import _repair_query
from apps.pii.store_registry import PlaceholderStore, registered_store, registered_stores, rewrite_json_path
from apps.tenants.models import PlaceholderMigrationCursor, Tenant

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 25
MAX_BATCH_SIZE = 100
LEASE_MINUTES = 15
_COUNT_SEPARATOR = "|"
_PROVISIONAL_PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)_\d+\]")

EXCLUDED_STORES = (
    ("lessons.TutoringSession", "messages"),
    ("friends.cross_tenant", "-"),
    ("journal.UserMemory", "memory"),
    ("unregistered", "-"),
)


class MigrationAuthoringError(RuntimeError):
    """A pre-scanned field could not be confirmed clean during rewrite."""


@dataclass(frozen=True)
class BatchResult:
    store_label: str
    mode: str
    done: bool
    skipped: bool
    busy: bool
    rows_scanned: int
    last_pk: str
    counts: dict[tuple[str, str], int]


def normalize_batch_size(value: int) -> int:
    if value <= 0:
        raise ValueError("batch_size must be positive")
    return min(value, MAX_BATCH_SIZE)


def _count_key(field: str, state: str) -> str:
    return f"{field}{_COUNT_SEPARATOR}{state}"


def _decode_counts(raw: dict[str, Any] | None) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for key, value in (raw or {}).items():
        if not isinstance(key, str) or _COUNT_SEPARATOR not in key or not isinstance(value, int):
            continue
        field, state = key.split(_COUNT_SEPARATOR, 1)
        counts[(field, state)] += value
    return counts


def _encode_counts(counts: Counter[tuple[str, str]]) -> dict[str, int]:
    return {_count_key(field, state): rows for (field, state), rows in sorted(counts.items()) if rows}


def emit_report(tenant_id: Any, store_label: str, counts: Iterable[tuple[tuple[str, str], int]]) -> None:
    for (field, state), rows in sorted(counts):
        logger.info(
            "w4_migration_report tenant=%s store=%s field=%s state=%s rows=%d",
            tenant_id,
            store_label,
            field,
            state,
            rows,
        )


def emit_exclusion_reports(tenant_id: Any) -> None:
    for store_label, field in EXCLUDED_STORES:
        emit_report(tenant_id, store_label, [((field, "skipped_by_design"), 0)])


def _receipt_state(receipts: Any, field: str) -> str:
    if not isinstance(receipts, dict):
        return "absent"
    receipt = receipts.get(field)
    if not isinstance(receipt, dict):
        return "absent"
    state = receipt.get("state")
    return state if isinstance(state, str) and state else "absent"


def repair_pending_count(tenant: Tenant, store: PlaceholderStore) -> int:
    """Count repair-eligible rows; terminal receipts are deliberately absent."""
    return (
        store.model.objects.filter(tenant=tenant)
        .filter(_repair_query(store.receipt_fields, store.receipts_field))
        .count()
    )


def _json_texts(value: Any, paths: tuple[tuple[str, ...], ...]) -> list[str]:
    texts: list[str] = []

    def collect(text: str) -> str:
        texts.append(text)
        return text

    walked = value
    for path in paths:
        walked, _changed = rewrite_json_path(walked, path, collect)
    return texts


def _texts_for_fields(row: Any, store: PlaceholderStore, fields: Iterable[str]) -> list[str]:
    texts: list[str] = []
    for field in fields:
        if field in store.flat_fields:
            value = getattr(row, field)
            if isinstance(value, str):
                texts.append(value)
        else:
            texts.extend(_json_texts(getattr(row, field), store.nested_json_paths(field)))
    return texts


def mint_owner_batch_entities(tenant: Tenant, texts: Iterable[str], *, commit: bool) -> int:
    """Detect a batch first, then mint its unique new entities in one lock.

    ``RedactionSession`` performs the same full owner NER/MINT_ALL policy but
    keeps provisional bindings in memory.  The transaction re-deduplicates
    against the locked tenant snapshot, so two rows with the same canonical
    name produce one durable binding and a concurrent writer cannot collide.
    """
    session = RedactionSession(tenant=tenant, mint=MINT_ALL)
    for text in texts:
        session.redact(text)

    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for provisional, entry in session.entity_map.items():
        match = _PROVISIONAL_PLACEHOLDER_RE.fullmatch(provisional)
        name = get_name(entry)
        key = canonical_key(name)
        if not match or not key:
            continue
        candidate = (match.group(1), name)
        canonical_candidate = (match.group(1), key)
        if canonical_candidate in seen:
            continue
        seen.add(canonical_candidate)
        candidates.append(candidate)

    if not commit or not candidates:
        return len(candidates)

    minted = 0
    with transaction.atomic():
        locked = Tenant.objects.select_for_update().only("pii_entity_map", "pii_type_counters").get(pk=tenant.pk)
        entity_map = dict(locked.pii_entity_map or {})
        counters = dict(locked.pii_type_counters or {})
        inverted = inverted_names_ci(entity_map, include_retired=False)
        for entity_type, name in candidates:
            key = canonical_key(name)
            if key in inverted:
                continue
            number = next_placeholder_number(entity_type, entity_map, counters)
            placeholder = f"[{entity_type}_{number}]"
            entity_map[placeholder] = to_storage_value(name)
            counters[entity_type] = number
            inverted[key] = (name, placeholder)
            minted += 1
        if minted:
            Tenant.objects.filter(pk=tenant.pk).update(
                pii_entity_map=entity_map,
                pii_type_counters=counters,
            )

    # The caller's tenant instance may predate a concurrent map write.  Always
    # carry the locked snapshot into the unlocked rewrite, even when this batch
    # did not itself mint anything.
    tenant.pii_entity_map = entity_map
    tenant.pii_type_counters = counters
    logger.info("w4_migration_mint tenant=%s entities=%d", tenant.pk, minted)
    return minted


def _has_updated_at(store: PlaceholderStore) -> bool:
    try:
        store.model._meta.get_field("updated_at")
    except FieldDoesNotExist:
        return False
    return True


def _row_queryset(tenant: Tenant, store: PlaceholderStore):
    fields = ["pk", "tenant", store.receipts_field, *store.flat_fields, *store.json_fields]
    if _has_updated_at(store):
        fields.append("updated_at")
    queryset = store.model.objects.filter(tenant=tenant).only(*dict.fromkeys(fields))
    if connection.vendor == "postgresql":
        return queryset.annotate(_w4_xmin=RawSQL("xmin::text", []))
    return queryset


def _fallback_version(row: Any, store: PlaceholderStore) -> tuple[Any, ...]:
    return tuple(repr(getattr(row, field)) for field in (*store.flat_fields, *store.json_fields, store.receipts_field))


def _row_version(row: Any, store: PlaceholderStore) -> Any:
    if _has_updated_at(store):
        if connection.vendor == "postgresql":
            return row.updated_at, row._w4_xmin
        return row.updated_at
    if connection.vendor == "postgresql":
        return row._w4_xmin
    return _fallback_version(row, store)


def _conditional_update(row: Any, store: PlaceholderStore, version: Any, updates: dict[str, Any]) -> int:
    queryset = store.model.objects.filter(pk=row.pk, tenant_id=row.tenant_id)
    if _has_updated_at(store):
        updated_at = version[0] if connection.vendor == "postgresql" else version
        queryset = queryset.filter(updated_at=updated_at)
        if connection.vendor == "postgresql":
            queryset = queryset.extra(where=["xmin::text = %s"], params=[version[1]])
        updates["updated_at"] = timezone.now()
    elif connection.vendor == "postgresql":
        queryset = queryset.extra(where=["xmin::text = %s"], params=[version])
    else:
        # Local SQLite fallback. Production/PostgreSQL uses xmin, which is an
        # atomic row-version CAS and catches changes to any column.
        queryset = queryset.filter(**{store.receipts_field: getattr(row, store.receipts_field)})
    return queryset.update(**updates)


def _claim_cursor(
    tenant: Tenant, store: PlaceholderStore, mode: str
) -> tuple[PlaceholderMigrationCursor, uuid.UUID] | None:
    now = timezone.now()
    token = uuid.uuid4()
    with transaction.atomic():
        cursor, _created = PlaceholderMigrationCursor.objects.select_for_update().get_or_create(
            tenant=tenant,
            store_label=store.model_label,
            mode=mode,
        )
        if cursor.lease_expires_at and cursor.lease_expires_at > now:
            return None
        cursor.status = PlaceholderMigrationCursor.Status.RUNNING
        cursor.lease_token = token
        cursor.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
        cursor.completed_at = None
        cursor.save(update_fields=["status", "lease_token", "lease_expires_at", "completed_at", "updated_at"])
    return cursor, token


def _release_cursor(
    cursor: PlaceholderMigrationCursor,
    token: uuid.UUID,
    *,
    status: str,
    last_pk: str | None = None,
    counts: Counter[tuple[str, str]] | None = None,
) -> PlaceholderMigrationCursor:
    with transaction.atomic():
        locked = PlaceholderMigrationCursor.objects.select_for_update().get(pk=cursor.pk)
        if locked.lease_token != token:
            return locked
        if last_pk is not None:
            locked.last_pk = last_pk
        if counts:
            aggregate = _decode_counts(locked.report_counts)
            aggregate.update(counts)
            locked.report_counts = _encode_counts(aggregate)
        locked.status = status
        locked.completed_at = timezone.now() if status == PlaceholderMigrationCursor.Status.COMPLETE else None
        locked.lease_token = None
        locked.lease_expires_at = None
        locked.save(
            update_fields=[
                "last_pk",
                "report_counts",
                "status",
                "completed_at",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        return locked


def reset_store_cursor(tenant: Tenant, store_label: str, *, commit: bool) -> None:
    mode = PlaceholderMigrationCursor.Mode.COMMIT if commit else PlaceholderMigrationCursor.Mode.DRY_RUN
    PlaceholderMigrationCursor.objects.filter(tenant=tenant, store_label=store_label, mode=mode).delete()


def process_store_batch(
    tenant: Tenant,
    store_label: str,
    *,
    commit: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BatchResult:
    """Process one bounded primary-key window for a registered store."""
    store = registered_store(store_label)
    batch_size = normalize_batch_size(batch_size)
    mode = PlaceholderMigrationCursor.Mode.COMMIT if commit else PlaceholderMigrationCursor.Mode.DRY_RUN
    claimed = _claim_cursor(tenant, store, mode)
    if claimed is None:
        return BatchResult(store_label, mode, False, False, True, 0, "", {})
    cursor, lease_token = claimed
    batch_counts: Counter[tuple[str, str]] = Counter()

    try:
        repair_rows = repair_pending_count(tenant, store)
        if repair_rows:
            counts = Counter({("-", "repair_pending_skipped"): repair_rows})
            released = _release_cursor(
                cursor,
                lease_token,
                status=PlaceholderMigrationCursor.Status.SKIPPED,
                counts=counts,
            )
            emit_report(tenant.pk, store.model_label, counts.items())
            return BatchResult(store_label, mode, False, True, False, 0, released.last_pk, dict(counts))

        queryset = _row_queryset(tenant, store).order_by("pk")
        if cursor.last_pk:
            queryset = queryset.filter(pk__gt=store.model._meta.pk.to_python(cursor.last_pk))
        scanned_rows = list(queryset[:batch_size])
        if not scanned_rows:
            released = _release_cursor(
                cursor,
                lease_token,
                status=PlaceholderMigrationCursor.Status.COMPLETE,
            )
            aggregate = _decode_counts(released.report_counts)
            emit_report(tenant.pk, store.model_label, aggregate.items())
            return BatchResult(store_label, mode, True, False, False, 0, released.last_pk, dict(aggregate))

        scan_versions = {row.pk: _row_version(row, store) for row in scanned_rows}
        eligible_fields: dict[Any, tuple[str, ...]] = {}
        prescan_texts: list[str] = []
        for row in scanned_rows:
            receipts = getattr(row, store.receipts_field, {})
            fields = tuple(field for field in store.receipt_fields if _receipt_state(receipts, field) != "placeholder")
            eligible_fields[row.pk] = fields
            prescan_texts.extend(_texts_for_fields(row, store, fields))

        mint_owner_batch_entities(tenant, prescan_texts, commit=commit)

        watermark = cursor.last_pk
        for scanned in scanned_rows:
            current = _row_queryset(tenant, store).get(pk=scanned.pk)
            fields = eligible_fields[scanned.pk]
            if _row_version(current, store) != scan_versions[scanned.pk]:
                for field in fields or ("-",):
                    batch_counts[(field, "changed_skipped")] += 1
                break

            receipts = dict(getattr(current, store.receipts_field, {}) or {})
            states = {field: _receipt_state(receipts, field) for field in store.receipt_fields}
            if not commit:
                for field, state in states.items():
                    batch_counts[(field, state)] += 1
                watermark = str(current.pk)
                continue

            updates: dict[str, Any] = {}
            for field in fields:
                if field in store.flat_fields:
                    authored = author_text(
                        tenant,
                        getattr(current, field),
                        seam=f"pii.migration.{store.model_label}.{field}",
                        writer="owner",
                        field=field,
                        live=False,
                        model_label=store.model_label,
                        _force_checked=True,
                        _mint_policy_override=MINT_NEVER,
                        _require_no_residual=True,
                    )
                    value = authored.text
                else:
                    authored = author_json_paths(
                        tenant,
                        getattr(current, field),
                        paths=store.nested_json_paths(field),
                        seam=f"pii.migration.{store.model_label}.{field}",
                        writer="owner",
                        field=field,
                        live=False,
                        model_label=store.model_label,
                        _force_checked=True,
                        _mint_policy_override=MINT_NEVER,
                        _require_no_residual=True,
                    )
                    value = authored.value
                if authored.receipt.get("state") != "placeholder":
                    batch_counts[(field, "authoring_unconfirmed")] += 1
                    raise MigrationAuthoringError(
                        f"migration authoring was not confirmed for {store.model_label}.{field} row={current.pk}"
                    )
                receipt = dict(authored.receipt)
                receipt["migrated"] = True
                receipts[field] = receipt
                updates[field] = value

            if fields:
                updates[store.receipts_field] = receipts
                if not _conditional_update(current, store, scan_versions[scanned.pk], updates):
                    for field in fields:
                        batch_counts[(field, "changed_skipped")] += 1
                    break

            for field, state in states.items():
                batch_counts[(field, state)] += 1
            watermark = str(current.pk)

        released = _release_cursor(
            cursor,
            lease_token,
            status=PlaceholderMigrationCursor.Status.PENDING,
            last_pk=watermark,
            counts=batch_counts,
        )
        return BatchResult(
            store_label,
            mode,
            False,
            False,
            False,
            len(scanned_rows),
            released.last_pk,
            dict(batch_counts),
        )
    except Exception:
        released = _release_cursor(
            cursor,
            lease_token,
            status=PlaceholderMigrationCursor.Status.PENDING,
            counts=batch_counts,
        )
        if batch_counts:
            emit_report(tenant.pk, store.model_label, batch_counts.items())
        logger.info(
            "w4_migration_batch tenant=%s store=%s mode=%s state=error last_pk=%s",
            tenant.pk,
            store.model_label,
            mode,
            released.last_pk,
        )
        raise


def migrate_tenant_registered_stores(
    tenant: Tenant,
    *,
    commit: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    store_label: str | None = None,
) -> dict[str, int]:
    """Synchronous management-command surface over the same batch engine."""
    stores = (registered_store(store_label),) if store_label else registered_stores()
    totals = {"stores_complete": 0, "stores_skipped": 0, "batches": 0}
    emit_exclusion_reports(tenant.pk)
    for store in stores:
        while True:
            result = process_store_batch(
                tenant,
                store.model_label,
                commit=commit,
                batch_size=batch_size,
            )
            if result.busy:
                break
            totals["batches"] += 1
            if result.skipped:
                totals["stores_skipped"] += 1
                break
            if any(state == "changed_skipped" for _field, state in result.counts):
                totals["stores_skipped"] += 1
                break
            if result.done:
                totals["stores_complete"] += 1
                break
    return totals


def _task_options(tenant_id: str, commit: bool, batch_size: int) -> tuple[Tenant, bool, int]:
    if type(commit) is not bool:
        raise ValueError("commit must be a JSON boolean")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ValueError("batch_size must be an integer")
    return Tenant.objects.get(pk=tenant_id), commit, normalize_batch_size(batch_size)


def historical_placeholder_migration_batch_task(
    tenant_id: str,
    store_label: str,
    *,
    commit: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """QStash batch task; chain only this store until its cursor completes."""
    tenant, commit, batch_size = _task_options(tenant_id, commit, batch_size)
    registered_store(store_label)
    result = process_store_batch(
        tenant,
        store_label,
        commit=commit,
        batch_size=batch_size,
    )
    from apps.cron.publish import publish_task

    if result.busy:
        publish_task(
            "historical_placeholder_migration_batch",
            str(tenant.pk),
            store_label,
            commit=commit,
            batch_size=batch_size,
            delay_seconds=LEASE_MINUTES * 60,
        )
    else:
        if result.done or result.skipped:
            publish_task(
                "historical_placeholder_migration_driver",
                str(tenant.pk),
                commit=commit,
                batch_size=batch_size,
                retry_skipped=False,
            )
        else:
            delay_seconds = 30 if any(state == "changed_skipped" for _field, state in result.counts) else None
            publish_task(
                "historical_placeholder_migration_batch",
                str(tenant.pk),
                store_label,
                commit=commit,
                batch_size=batch_size,
                delay_seconds=delay_seconds,
            )
    return {
        "tenant_id": str(tenant.pk),
        "store": store_label,
        "mode": result.mode,
        "done": result.done,
        "skipped": result.skipped,
        "busy": result.busy,
        "rows_scanned": result.rows_scanned,
        "last_pk": result.last_pk,
    }


def historical_placeholder_migration_driver_task(
    tenant_id: str,
    *,
    commit: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retry_skipped: bool = True,
) -> dict[str, Any]:
    """QStash driver that admits exactly one registered store at a time."""
    tenant, commit, batch_size = _task_options(tenant_id, commit, batch_size)
    if type(retry_skipped) is not bool:
        raise ValueError("retry_skipped must be a JSON boolean")
    mode = PlaceholderMigrationCursor.Mode.COMMIT if commit else PlaceholderMigrationCursor.Mode.DRY_RUN
    if retry_skipped:
        PlaceholderMigrationCursor.objects.filter(
            tenant=tenant,
            mode=mode,
            status=PlaceholderMigrationCursor.Status.SKIPPED,
        ).update(status=PlaceholderMigrationCursor.Status.PENDING)
        emit_exclusion_reports(tenant.pk)

    now = timezone.now()
    skipped = 0
    for store in registered_stores():
        cursor, _created = PlaceholderMigrationCursor.objects.get_or_create(
            tenant=tenant,
            store_label=store.model_label,
            mode=mode,
        )
        if cursor.status == PlaceholderMigrationCursor.Status.COMPLETE:
            continue
        if cursor.status == PlaceholderMigrationCursor.Status.SKIPPED:
            skipped += 1
            continue
        if cursor.lease_expires_at and cursor.lease_expires_at > now:
            return {
                "tenant_id": str(tenant.pk),
                "mode": mode,
                "status": "busy",
                "store": store.model_label,
            }

        from apps.cron.publish import publish_task

        publish_task(
            "historical_placeholder_migration_batch",
            str(tenant.pk),
            store.model_label,
            commit=commit,
            batch_size=batch_size,
        )
        return {
            "tenant_id": str(tenant.pk),
            "mode": mode,
            "status": "chained",
            "store": store.model_label,
        }

    logger.info(
        "w4_migration_driver tenant=%s mode=%s state=complete stores=%d skipped=%d",
        tenant.pk,
        mode,
        len(registered_stores()),
        skipped,
    )
    return {
        "tenant_id": str(tenant.pk),
        "mode": mode,
        "status": "complete",
        "stores": len(registered_stores()),
        "skipped": skipped,
    }
