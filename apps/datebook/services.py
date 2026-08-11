"""Transactional generational-sync, gateway, disable, and command services."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time

from apps.common.tenant_tz import tenant_tz
from apps.orchestrator.envelope_registry import suppress_refresh
from apps.pii.store_authoring import author_store_fields
from apps.tenants.models import Tenant

from .hashing import (
    HASH_RE,
    SOURCE_KEY_RE,
    ItemValidationError,
    canonical_json_bytes,
    clean_event_item,
    clean_reminder_item,
    manifest_digest_v1,
    normalize_text,
)
from .models import (
    AuthorizationStatus,
    DatebookGateway,
    DeviceCommand,
    MirrorEvent,
    MirrorReminder,
    SyncPage,
    SyncRun,
    TimeKind,
)
from .readiness import datebook_delivery_ready

logger = logging.getLogger(__name__)

EVENT_WINDOW_PAST_DAYS = 30
EVENT_WINDOW_FUTURE_DAYS = 180
MAX_PAGE_ITEMS = 50
MAX_SCOPE_ITEMS = 10_000
COMMAND_LEASE_SECONDS = 90
COMMAND_EXECUTION_TIMEOUT_SECONDS = 10 * 60
COMMAND_TTL_HOURS = 72
COMMAND_DAILY_ITEM_CAP = 20
COMMAND_PAYLOAD_BYTES = 32_000


@dataclass
class ProtocolError(Exception):
    code: str
    status_code: int = 400
    extra: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.code

    def as_data(self) -> dict:
        return {"error": self.code, **self.extra}


class _CommitStopped(Exception):
    """Internal control flow: commit deliberate state changes, then return an error."""


def _digest(value) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ProtocolError("invalid_body") from exc
    return hashlib.sha256(encoded).hexdigest()


def _positive_epoch(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProtocolError("invalid_gateway_epoch")
    return value


def _installation_id(value) -> str:
    if not isinstance(value, str):
        raise ProtocolError("invalid_installation_id")
    value = value.strip()
    if not value or len(value) > 64:
        raise ProtocolError("invalid_installation_id")
    return value


def _locked_active_gateway(tenant) -> DatebookGateway:
    gateway = (
        DatebookGateway.objects.select_for_update().filter(tenant=tenant, status=DatebookGateway.Status.ACTIVE).first()
    )
    if gateway is None:
        raise ProtocolError("gateway_not_registered", 409)
    return gateway


def _assert_gateway(gateway, *, installation_id: str, gateway_epoch: int) -> None:
    if gateway.installation_id != installation_id or gateway.gateway_epoch != gateway_epoch:
        raise ProtocolError(
            "stale_gateway",
            409,
            {"gateway_epoch": gateway.gateway_epoch},
        )


def _cancel_never_started_commands(tenant, now) -> None:
    from apps.actions.models import ActionAuditOutcome
    from apps.actions.services import record_datebook_command_transition

    commands = list(
        DeviceCommand.objects.select_for_update().filter(
            tenant=tenant,
            state__in=[DeviceCommand.State.PENDING, DeviceCommand.State.LEASED],
            started_at__isnull=True,
        )
    )
    for command in commands:
        command.state = DeviceCommand.State.CANCELLED
        command.lease_token = None
        command.lease_expires_at = None
        command.resolved_at = now
        command.save(
            update_fields=[
                "state",
                "lease_token",
                "lease_expires_at",
                "resolved_at",
                "updated_at",
            ]
        )
        record_datebook_command_transition(command, ActionAuditOutcome.CANCELLED)


def _abort_active_sync_runs(tenant) -> None:
    for run in SyncRun.objects.select_for_update().filter(
        tenant=tenant,
        state__in=[SyncRun.State.OPEN, SyncRun.State.STAGED],
    ):
        _abort_run(run, full_snapshot=True)


def register_gateway(tenant, *, installation_id, takeover: bool) -> tuple[DatebookGateway, bool]:
    installation_id = _installation_id(installation_id)
    if not isinstance(takeover, bool):
        raise ProtocolError("invalid_takeover")
    now = timezone.now()

    with suppress_refresh(), transaction.atomic():
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        active = (
            DatebookGateway.objects.select_for_update()
            .filter(tenant=tenant, status=DatebookGateway.Status.ACTIVE)
            .first()
        )
        if active is not None and active.installation_id == installation_id:
            active.last_seen_at = now
            active.save(update_fields=["last_seen_at", "updated_at"])
            return active, False
        if active is not None and not takeover:
            raise ProtocolError(
                "stale_gateway",
                409,
                {"gateway_epoch": active.gateway_epoch, "takeover_required": True},
            )

        max_epoch = DatebookGateway.objects.filter(tenant=tenant).aggregate(value=Max("gateway_epoch"))["value"] or 0
        next_epoch = max_epoch + 1
        current_generation = (
            active.current_generation
            if active is not None
            else DatebookGateway.objects.filter(tenant=tenant).aggregate(value=Max("current_generation"))["value"] or 0
        )
        if active is not None:
            active.status = DatebookGateway.Status.RETIRED
            active.retired_at = now
            active.save(update_fields=["status", "retired_at", "updated_at"])
            _abort_active_sync_runs(tenant)
            _cancel_never_started_commands(tenant, now)

        gateway = (
            DatebookGateway.objects.select_for_update().filter(tenant=tenant, installation_id=installation_id).first()
        )
        if gateway is None:
            gateway = DatebookGateway.objects.create(
                tenant=tenant,
                installation_id=installation_id,
                gateway_epoch=next_epoch,
                current_generation=current_generation,
                status=DatebookGateway.Status.ACTIVE,
                last_seen_at=now,
            )
        else:
            gateway.gateway_epoch = next_epoch
            gateway.current_generation = current_generation
            gateway.events_full_snapshot_required = True
            gateway.reminders_full_snapshot_required = True
            gateway.events_authorization = AuthorizationStatus.NOT_DETERMINED
            gateway.reminders_authorization = AuthorizationStatus.NOT_DETERMINED
            gateway.events_last_complete_sync_at = None
            gateway.reminders_last_complete_sync_at = None
            gateway.events_window_start = None
            gateway.events_window_end = None
            gateway.status = DatebookGateway.Status.ACTIVE
            gateway.retired_at = None
            gateway.last_seen_at = now
            gateway.save(
                update_fields=[
                    "gateway_epoch",
                    "current_generation",
                    "events_full_snapshot_required",
                    "reminders_full_snapshot_required",
                    "events_authorization",
                    "reminders_authorization",
                    "events_last_complete_sync_at",
                    "reminders_last_complete_sync_at",
                    "events_window_start",
                    "events_window_end",
                    "status",
                    "retired_at",
                    "last_seen_at",
                    "updated_at",
                ]
            )
        return gateway, active is not None


def disable_datebook(tenant, *, purge: bool) -> None:
    """Fail closed, invalidate device epochs, and optionally erase the mirror."""

    if not isinstance(purge, bool):
        raise ValueError("purge must be a boolean")
    now = timezone.now()
    with transaction.atomic():
        locked_tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
        locked_tenant.datebook_enabled = False
        locked_tenant.save(update_fields=["datebook_enabled", "updated_at"])
        gateway = (
            DatebookGateway.objects.select_for_update()
            .filter(tenant=locked_tenant, status=DatebookGateway.Status.ACTIVE)
            .first()
        )
        if gateway is not None:
            gateway.gateway_epoch += 1
            gateway.events_full_snapshot_required = True
            gateway.reminders_full_snapshot_required = True
            gateway.save(
                update_fields=[
                    "gateway_epoch",
                    "events_full_snapshot_required",
                    "reminders_full_snapshot_required",
                    "updated_at",
                ]
            )
        _abort_active_sync_runs(locked_tenant)
        _cancel_never_started_commands(locked_tenant, now)
        if purge:
            MirrorEvent.objects.filter(tenant=locked_tenant).delete()
            MirrorReminder.objects.filter(tenant=locked_tenant).delete()


def _scope_declaration(raw, *, consented: bool) -> tuple[bool, str, bool, bool, bool]:
    if not consented:
        return False, AuthorizationStatus.NOT_DETERMINED, False, False, False
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProtocolError("invalid_scope_declaration")
    authorization = raw.get("authorization", AuthorizationStatus.NOT_DETERMINED)
    coverage_complete = raw.get("coverage_complete", False)
    full_snapshot = raw.get("full_snapshot", False)
    if authorization not in AuthorizationStatus.values:
        raise ProtocolError("invalid_authorization")
    if not isinstance(coverage_complete, bool):
        raise ProtocolError("invalid_coverage_complete")
    if not isinstance(full_snapshot, bool):
        raise ProtocolError("invalid_full_snapshot")
    committable = authorization == AuthorizationStatus.FULL_ACCESS and coverage_complete
    return True, authorization, coverage_complete, committable, full_snapshot


def open_sync_run(
    tenant,
    *,
    installation_id,
    gateway_epoch,
    client_run_id,
    events,
    reminders,
) -> tuple[SyncRun, bool]:
    installation_id = _installation_id(installation_id)
    gateway_epoch = _positive_epoch(gateway_epoch)
    if not isinstance(client_run_id, str) or not client_run_id.strip() or len(client_run_id.strip()) > 64:
        raise ProtocolError("invalid_client_run_id")
    client_run_id = client_run_id.strip()

    event_scope = _scope_declaration(events, consented=tenant.datebook_events_consent_at is not None)
    reminder_scope = _scope_declaration(reminders, consented=tenant.datebook_reminders_consent_at is not None)
    now = timezone.now().astimezone(UTC).replace(microsecond=0)

    with transaction.atomic():
        gateway = _locked_active_gateway(tenant)
        _assert_gateway(gateway, installation_id=installation_id, gateway_epoch=gateway_epoch)
        existing = SyncRun.objects.filter(tenant=tenant, client_run_id=client_run_id).first()
        if existing is not None:
            if existing.gateway_id != gateway.id or existing.gateway_epoch != gateway.gateway_epoch:
                raise ProtocolError("client_run_conflict", 409)
            existing_scopes = (
                existing.events_in_scope,
                existing.events_authorization,
                existing.events_coverage_complete,
                existing.events_committable,
                existing.reminders_in_scope,
                existing.reminders_authorization,
                existing.reminders_coverage_complete,
                existing.reminders_committable,
            )
            requested_scopes = (*event_scope[:4], *reminder_scope[:4])
            if existing_scopes != requested_scopes:
                raise ProtocolError("client_run_conflict", 409)
            return existing, False
        try:
            run = SyncRun.objects.create(
                tenant=tenant,
                gateway=gateway,
                client_run_id=client_run_id,
                server_now=now,
                event_window_start=now - timedelta(days=EVENT_WINDOW_PAST_DAYS),
                event_window_end=now + timedelta(days=EVENT_WINDOW_FUTURE_DAYS),
                base_generation=gateway.current_generation,
                gateway_epoch=gateway.gateway_epoch,
                events_in_scope=event_scope[0],
                events_authorization=event_scope[1],
                events_coverage_complete=event_scope[2],
                events_committable=event_scope[3],
                events_full_snapshot=event_scope[4] or gateway.events_full_snapshot_required,
                reminders_in_scope=reminder_scope[0],
                reminders_authorization=reminder_scope[1],
                reminders_coverage_complete=reminder_scope[2],
                reminders_committable=reminder_scope[3],
                reminders_full_snapshot=reminder_scope[4] or gateway.reminders_full_snapshot_required,
            )
        except IntegrityError:
            run = SyncRun.objects.get(tenant=tenant, client_run_id=client_run_id)
            if run.gateway_id != gateway.id or run.gateway_epoch != gateway.gateway_epoch:
                raise ProtocolError("client_run_conflict", 409) from None
            existing_scopes = (
                run.events_in_scope,
                run.events_authorization,
                run.events_coverage_complete,
                run.events_committable,
                run.reminders_in_scope,
                run.reminders_authorization,
                run.reminders_coverage_complete,
                run.reminders_committable,
            )
            if existing_scopes != (*event_scope[:4], *reminder_scope[:4]):
                raise ProtocolError("client_run_conflict", 409) from None
            return run, False
        gateway.last_seen_at = now
        gateway.save(update_fields=["last_seen_at", "updated_at"])
        return run, True


def _author_staged_item(tenant, clean: dict, *, model_label: str) -> dict:
    authored, receipts = author_store_fields(
        tenant,
        clean,
        model_label=model_label,
        seam="datebook.owner.eventkit.ingress",
        writer="owner",
    )
    authored["pii_receipts"] = receipts
    return authored


def _clean_page_items(items, cleaner) -> tuple[list[dict], list[dict]]:
    clean: list[dict] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        try:
            normalized = cleaner(item)
            if normalized["source_key"] in seen:
                raise ItemValidationError("duplicate_source_key")
            seen.add(normalized["source_key"])
            clean.append(normalized)
        except ItemValidationError as exc:
            errors.append({"index": index, "error": exc.code})
    return clean, errors


def _page_response(page: SyncPage, *, idempotent: bool) -> dict:
    return {
        "run_id": str(page.run_id),
        "page_index": page.page_index,
        "idempotent": idempotent,
        "events": {
            "accepted": page.event_count if page.events_valid else 0,
            "committable": page.events_valid and page.run.events_committable,
            "errors": (page.error_codes or {}).get("events", []),
        },
        "reminders": {
            "accepted": page.reminder_count if page.reminders_valid else 0,
            "committable": page.reminders_valid and page.run.reminders_committable,
            "errors": (page.error_codes or {}).get("reminders", []),
        },
    }


def _event_payload_range(payload: dict, tenant) -> tuple[datetime, datetime]:
    kind = payload["time_kind"]
    if kind == TimeKind.ZONED:
        return parse_datetime(payload["zoned_start_at"]), parse_datetime(payload["zoned_end_at"])
    tz = tenant_tz(tenant)
    if kind == TimeKind.ALL_DAY:
        start = datetime.combine(parse_date(payload["all_day_start_date"]), time.min, tzinfo=tz)
        end = datetime.combine(parse_date(payload["all_day_end_date_exclusive"]), time.min, tzinfo=tz)
        return start.astimezone(UTC), end.astimezone(UTC)
    start = datetime.combine(
        parse_date(payload["floating_start_date"]),
        parse_time(payload["floating_start_time"]),
        tzinfo=tz,
    )
    end = datetime.combine(
        parse_date(payload["floating_end_date"]),
        parse_time(payload["floating_end_time"]),
        tzinfo=tz,
    )
    return start.astimezone(UTC), end.astimezone(UTC)


def event_overlaps_window(event: MirrorEvent, tenant, window_start, window_end) -> bool:
    if event.time_kind == TimeKind.ZONED:
        start, end = event.zoned_start_at, event.zoned_end_at
    else:
        payload = {
            "time_kind": event.time_kind,
            "all_day_start_date": event.all_day_start_date.isoformat() if event.all_day_start_date else None,
            "all_day_end_date_exclusive": (
                event.all_day_end_date_exclusive.isoformat() if event.all_day_end_date_exclusive else None
            ),
            "zoned_start_at": None,
            "zoned_end_at": None,
            "floating_start_date": event.floating_start_date.isoformat() if event.floating_start_date else None,
            "floating_start_time": event.floating_start_time.isoformat() if event.floating_start_time else None,
            "floating_end_date": event.floating_end_date.isoformat() if event.floating_end_date else None,
            "floating_end_time": event.floating_end_time.isoformat() if event.floating_end_time else None,
        }
        start, end = _event_payload_range(payload, tenant)
    return start < window_end and end > window_start


def _stage_rows(model, tenant, run, page_index: int, payloads: list[dict]) -> list[dict]:
    if not payloads:
        return []
    keys = [payload["source_key"] for payload in payloads]
    existing = {
        row.source_key: row for row in model.objects.select_for_update().filter(tenant=tenant, source_key__in=keys)
    }
    errors = []
    for index, payload in enumerate(payloads):
        row = existing.get(payload["source_key"])
        if row is not None and row.staged_run_id is not None:
            errors.append({"index": index, "error": "source_key_already_staged"})
    if errors:
        return errors
    for payload in payloads:
        row = existing.get(payload["source_key"])
        if row is None:
            row = model(tenant=tenant, source_key=payload["source_key"])
        row.staged_run = run
        row.staged_page_index = page_index
        row.staged_payload = payload
        row.save()
    return []


def stage_sync_page(
    tenant,
    *,
    run_id,
    page_index,
    installation_id,
    gateway_epoch,
    events,
    reminders,
) -> dict:
    installation_id = _installation_id(installation_id)
    gateway_epoch = _positive_epoch(gateway_epoch)
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        raise ProtocolError("invalid_page_index")
    if not isinstance(events, list) or not isinstance(reminders, list):
        raise ProtocolError("invalid_page_items")
    if len(events) > MAX_PAGE_ITEMS:
        raise ProtocolError("too_many_events")
    if len(reminders) > MAX_PAGE_ITEMS:
        raise ProtocolError("too_many_reminders")
    request_digest = _digest({"events": events, "reminders": reminders})

    event_clean, event_errors = _clean_page_items(events, clean_event_item)
    reminder_clean, reminder_errors = _clean_page_items(reminders, clean_reminder_item)
    with suppress_refresh(), transaction.atomic():
        locked_tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
        gateway = _locked_active_gateway(locked_tenant)
        _assert_gateway(gateway, installation_id=installation_id, gateway_epoch=gateway_epoch)
        try:
            run = SyncRun.objects.select_for_update().get(id=run_id, tenant=locked_tenant)
        except (SyncRun.DoesNotExist, ValueError, TypeError) as exc:
            raise ProtocolError("run_not_found", 404) from exc
        if run.gateway_id != gateway.id or run.gateway_epoch != gateway.gateway_epoch:
            raise ProtocolError("stale_gateway", 409, {"gateway_epoch": gateway.gateway_epoch})
        if run.state not in [SyncRun.State.OPEN, SyncRun.State.STAGED]:
            raise ProtocolError("run_not_open", 409)
        prior = SyncPage.objects.filter(run=run, page_index=page_index).first()
        if prior is not None:
            if prior.request_digest != request_digest:
                raise ProtocolError("page_conflict", 409)
            return _page_response(prior, idempotent=True)

        if events and not run.events_in_scope:
            event_errors.append({"index": None, "error": "scope_not_requested"})
        if reminders and not run.reminders_in_scope:
            reminder_errors.append({"index": None, "error": "scope_not_requested"})
        if events and run.events_in_scope and not run.events_committable:
            event_errors.append({"index": None, "error": "scope_not_committable"})
        if reminders and run.reminders_in_scope and not run.reminders_committable:
            reminder_errors.append({"index": None, "error": "scope_not_committable"})

        totals = SyncPage.objects.filter(run=run).aggregate(events=Sum("event_count"), reminders=Sum("reminder_count"))
        if (totals["events"] or 0) + len(events) > MAX_SCOPE_ITEMS:
            event_errors.append({"index": None, "error": "snapshot_item_cap"})
        if (totals["reminders"] or 0) + len(reminders) > MAX_SCOPE_ITEMS:
            reminder_errors.append({"index": None, "error": "snapshot_item_cap"})

        if not event_errors:
            outside = [
                {"index": index, "error": "event_outside_window"}
                for index, payload in enumerate(event_clean)
                if not (
                    (bounds := _event_payload_range(payload, locked_tenant))[0] < run.event_window_end
                    and bounds[1] > run.event_window_start
                )
            ]
            event_errors.extend(outside)
        if not event_errors:
            event_authored = [
                _author_staged_item(locked_tenant, item, model_label="datebook.MirrorEvent") for item in event_clean
            ]
            event_errors.extend(_stage_rows(MirrorEvent, locked_tenant, run, page_index, event_authored))
        if not reminder_errors:
            reminder_authored = [
                _author_staged_item(locked_tenant, item, model_label="datebook.MirrorReminder")
                for item in reminder_clean
            ]
            reminder_errors.extend(_stage_rows(MirrorReminder, locked_tenant, run, page_index, reminder_authored))

        events_valid = not event_errors
        reminders_valid = not reminder_errors
        if not events_valid:
            run.events_committable = False
        if not reminders_valid:
            run.reminders_committable = False
        now = timezone.now()
        run.state = SyncRun.State.STAGED
        run.staged_at = run.staged_at or now
        run.save(
            update_fields=[
                "events_committable",
                "reminders_committable",
                "state",
                "staged_at",
                "updated_at",
            ]
        )
        page = SyncPage.objects.create(
            tenant=tenant,
            run=run,
            page_index=page_index,
            request_digest=request_digest,
            event_count=len(events),
            reminder_count=len(reminders),
            events_valid=events_valid,
            reminders_valid=reminders_valid,
            error_codes={"events": event_errors, "reminders": reminder_errors},
        )
        return _page_response(page, idempotent=False)


def _parse_commit_scope(raw) -> dict:
    if not isinstance(raw, dict):
        raise ProtocolError("missing_manifest")
    item_count = raw.get("item_count")
    digest = raw.get("manifest_digest")
    absent = raw.get("absent_source_keys", [])
    if isinstance(item_count, bool) or not isinstance(item_count, int) or not 0 <= item_count <= MAX_SCOPE_ITEMS:
        raise ProtocolError("invalid_manifest_count")
    if not isinstance(digest, str) or HASH_RE.fullmatch(digest) is None:
        raise ProtocolError("invalid_manifest_digest")
    if not isinstance(absent, list) or len(absent) > MAX_SCOPE_ITEMS:
        raise ProtocolError("invalid_absent_source_keys")
    if any(not isinstance(key, str) or SOURCE_KEY_RE.fullmatch(key) is None for key in absent):
        raise ProtocolError("invalid_absent_source_keys")
    if len(absent) != len(set(absent)):
        raise ProtocolError("duplicate_absent_source_key")
    return {"item_count": item_count, "manifest_digest": digest, "absent_source_keys": absent}


def _clear_staging(model, run) -> None:
    staged = model.objects.filter(staged_run=run)
    staged.filter(content_hash="").delete()
    staged.update(staged_run=None, staged_page_index=None, staged_payload={})


def _abort_run(run, *, full_snapshot: bool) -> None:
    now = timezone.now()
    _clear_staging(MirrorEvent, run)
    _clear_staging(MirrorReminder, run)
    run.state = SyncRun.State.ABORTED
    run.aborted_at = now
    run.requires_full_snapshot = full_snapshot
    run.save(update_fields=["state", "aborted_at", "requires_full_snapshot", "updated_at"])


def _prospective_events(tenant, run, staged_rows, absent: set[str]) -> dict[str, str]:
    result = {}
    if not run.events_full_snapshot:
        for row in MirrorEvent.objects.select_for_update().filter(tenant=tenant, active=True):
            if row.source_key not in absent and event_overlaps_window(
                row, tenant, run.event_window_start, run.event_window_end
            ):
                result[row.source_key] = row.content_hash
    for row in staged_rows:
        if row.source_key in absent:
            raise ProtocolError("staged_item_marked_absent", 409)
        result[row.source_key] = row.staged_payload["content_hash"]
    return result


def _prospective_reminders(tenant, run, staged_rows, absent: set[str]) -> dict[str, str]:
    result = {}
    if not run.reminders_full_snapshot:
        result = {
            source_key: content_hash
            for source_key, content_hash in MirrorReminder.objects.select_for_update()
            .filter(tenant=tenant, active=True)
            .values_list("source_key", "content_hash")
            if source_key not in absent
        }
    for row in staged_rows:
        if row.source_key in absent:
            raise ProtocolError("staged_item_marked_absent", 409)
        result[row.source_key] = row.staged_payload["content_hash"]
    return result


def _event_model_fields(payload: dict) -> dict:
    fields = dict(payload)
    fields.pop("source_key", None)
    fields.pop("pii_receipts", None)
    for key in ("all_day_start_date", "all_day_end_date_exclusive", "floating_start_date", "floating_end_date"):
        fields[key] = parse_date(fields[key]) if fields[key] else None
    for key in ("floating_start_time", "floating_end_time"):
        fields[key] = parse_time(fields[key]) if fields[key] else None
    for key in ("zoned_start_at", "zoned_end_at"):
        fields[key] = parse_datetime(fields[key]) if fields[key] else None
    return fields


def _reminder_model_fields(payload: dict) -> dict:
    fields = dict(payload)
    fields.pop("source_key", None)
    fields.pop("pii_receipts", None)
    for key in ("due_date", "floating_due_date"):
        fields[key] = parse_date(fields[key]) if fields[key] else None
    if fields["floating_due_time"]:
        fields["floating_due_time"] = parse_time(fields["floating_due_time"])
    if fields["zoned_due_at"]:
        fields["zoned_due_at"] = parse_datetime(fields["zoned_due_at"])
    if fields["completed_at"]:
        fields["completed_at"] = parse_datetime(fields["completed_at"])
    return fields


def _publish_staged_rows(
    model, tenant, staged_rows, prospective: dict[str, str], generation: int, field_builder
) -> None:
    live_fields = [
        field.name
        for field in model._meta.fields
        if field.name
        not in {
            "id",
            "tenant",
            "source_key",
            "active",
            "first_seen_generation",
            "last_seen_generation",
            "inactive_generation",
            "staged_run",
            "staged_page_index",
            "staged_payload",
            "pii_receipts",
            "created_at",
            "updated_at",
        }
    ]
    for row in staged_rows:
        payload = row.staged_payload
        for key, value in field_builder(payload).items():
            setattr(row, key, value)
        row.pii_receipts = payload["pii_receipts"]
        row.active = True
        if row.first_seen_generation == 0:
            row.first_seen_generation = generation
        row.last_seen_generation = generation
        row.inactive_generation = None
        row.staged_run = None
        row.staged_page_index = None
        row.staged_payload = {}
        row.save(
            update_fields=[
                *live_fields,
                "pii_receipts",
                "active",
                "first_seen_generation",
                "last_seen_generation",
                "inactive_generation",
                "staged_run",
                "staged_page_index",
                "staged_payload",
                "updated_at",
            ]
        )
    model.objects.filter(tenant=tenant, source_key__in=prospective, active=True).update(
        last_seen_generation=generation,
        inactive_generation=None,
    )
    model.objects.filter(tenant=tenant, active=True).exclude(source_key__in=prospective).update(
        active=False,
        inactive_generation=generation,
    )


def push_visibility_refresh(_tenant_id: str) -> None:
    """Push one committed mirror generation with the shared zero-debounce key."""

    def _run() -> None:
        try:
            from apps.orchestrator.workspace_envelope import push_user_md

            push_user_md(_tenant_id, debounce_seconds=0)
        except Exception:
            logger.warning(
                "Post-sync USER.md push failed for tenant %s",
                str(_tenant_id)[:8],
                exc_info=True,
            )

    from django.conf import settings

    if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
        _run()
        return
    threading.Thread(target=_run, daemon=True).start()


def commit_sync_run(
    tenant,
    *,
    run_id,
    installation_id,
    gateway_epoch,
    events,
    reminders,
) -> dict:
    installation_id = _installation_id(installation_id)
    gateway_epoch = _positive_epoch(gateway_epoch)
    request_digest = _digest({"events": events, "reminders": reminders})
    deferred_error: ProtocolError | None = None
    response: dict | None = None

    # _CommitStopped is suppressed inside atomic so deliberate abort/cleanup
    # commits before the typed protocol error is raised outside the block.
    with suppress_refresh(), transaction.atomic(), suppress(_CommitStopped):
        locked_tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
        gateway = _locked_active_gateway(locked_tenant)
        _assert_gateway(gateway, installation_id=installation_id, gateway_epoch=gateway_epoch)
        try:
            run = SyncRun.objects.select_for_update().get(id=run_id, tenant=locked_tenant)
        except (SyncRun.DoesNotExist, ValueError, TypeError) as exc:
            raise ProtocolError("run_not_found", 404) from exc
        if run.gateway_id != gateway.id or run.gateway_epoch != gateway.gateway_epoch:
            raise ProtocolError("stale_gateway", 409, {"gateway_epoch": gateway.gateway_epoch})
        if run.state == SyncRun.State.COMMITTED:
            if run.commit_request_digest != request_digest:
                raise ProtocolError("commit_conflict", 409)
            return {
                "run_id": str(run.id),
                "generation": run.published_generation,
                "idempotent": True,
                "events": "committed" if run.events_committable else "not_committed",
                "reminders": "committed" if run.reminders_committable else "not_committed",
            }
        if run.state == SyncRun.State.ABORTED:
            code = "full_snapshot_required" if run.requires_full_snapshot else "run_aborted"
            raise ProtocolError(code, 409)
        if gateway.current_generation != run.base_generation:
            _abort_run(run, full_snapshot=False)
            deferred_error = ProtocolError(
                "stale_base_generation",
                409,
                {"current_generation": gateway.current_generation},
            )
            raise _CommitStopped

        auth_update_fields = []
        if run.events_in_scope:
            gateway.events_authorization = run.events_authorization
            auth_update_fields.append("events_authorization")
        if run.reminders_in_scope:
            gateway.reminders_authorization = run.reminders_authorization
            auth_update_fields.append("reminders_authorization")
        if auth_update_fields:
            gateway.save(update_fields=[*auth_update_fields, "updated_at"])

        if run.events_committable and not isinstance(events, dict):
            raise ProtocolError("missing_events_manifest")
        if run.reminders_committable and not isinstance(reminders, dict):
            raise ProtocolError("missing_reminders_manifest")
        event_manifest = _parse_commit_scope(events) if run.events_committable else None
        reminder_manifest = _parse_commit_scope(reminders) if run.reminders_committable else None
        if not run.events_committable and not run.reminders_committable:
            _abort_run(run, full_snapshot=False)
            deferred_error = ProtocolError(
                "no_committable_scopes",
                409,
                {
                    "events": "not_committed",
                    "reminders": "not_committed",
                },
            )
        else:
            staged_events = list(MirrorEvent.objects.select_for_update().filter(staged_run=run))
            staged_reminders = list(MirrorReminder.objects.select_for_update().filter(staged_run=run))
            prospective_events: dict[str, str] = {}
            prospective_reminders: dict[str, str] = {}
            mismatch_scopes = []

            if run.events_committable:
                event_absent = set(event_manifest["absent_source_keys"])
                prospective_events = _prospective_events(locked_tenant, run, staged_events, event_absent)
                actual_digest = manifest_digest_v1(prospective_events.items())
                if (
                    len(prospective_events) != event_manifest["item_count"]
                    or actual_digest != event_manifest["manifest_digest"]
                ):
                    mismatch_scopes.append("events")
            if run.reminders_committable:
                reminder_absent = set(reminder_manifest["absent_source_keys"])
                prospective_reminders = _prospective_reminders(locked_tenant, run, staged_reminders, reminder_absent)
                actual_digest = manifest_digest_v1(prospective_reminders.items())
                if (
                    len(prospective_reminders) != reminder_manifest["item_count"]
                    or actual_digest != reminder_manifest["manifest_digest"]
                ):
                    mismatch_scopes.append("reminders")

            if mismatch_scopes:
                if "events" in mismatch_scopes:
                    gateway.events_full_snapshot_required = True
                if "reminders" in mismatch_scopes:
                    gateway.reminders_full_snapshot_required = True
                gateway.save(
                    update_fields=[
                        "events_full_snapshot_required",
                        "reminders_full_snapshot_required",
                        "updated_at",
                    ]
                )
                _abort_run(run, full_snapshot=True)
                deferred_error = ProtocolError(
                    "full_snapshot_required",
                    409,
                    {"reason": "manifest_mismatch", "scopes": mismatch_scopes},
                )
            else:
                generation = gateway.current_generation + 1
                if run.events_committable:
                    _publish_staged_rows(
                        MirrorEvent,
                        locked_tenant,
                        staged_events,
                        prospective_events,
                        generation,
                        _event_model_fields,
                    )
                    gateway.events_last_complete_sync_at = timezone.now()
                    gateway.events_window_start = run.event_window_start
                    gateway.events_window_end = run.event_window_end
                    gateway.events_full_snapshot_required = False
                if run.reminders_committable:
                    _publish_staged_rows(
                        MirrorReminder,
                        locked_tenant,
                        staged_reminders,
                        prospective_reminders,
                        generation,
                        _reminder_model_fields,
                    )
                    gateway.reminders_last_complete_sync_at = timezone.now()
                    gateway.reminders_full_snapshot_required = False
                _clear_staging(MirrorEvent, run)
                _clear_staging(MirrorReminder, run)

                if run.events_in_scope:
                    gateway.events_authorization = run.events_authorization
                if run.reminders_in_scope:
                    gateway.reminders_authorization = run.reminders_authorization
                gateway.current_generation = generation
                gateway.last_seen_at = timezone.now()
                gateway.save()

                now = timezone.now()
                run.events_manifest_digest = event_manifest["manifest_digest"] if event_manifest else ""
                run.events_item_count = event_manifest["item_count"] if event_manifest else None
                run.events_absent_source_keys = event_manifest["absent_source_keys"] if event_manifest else []
                run.reminders_manifest_digest = reminder_manifest["manifest_digest"] if reminder_manifest else ""
                run.reminders_item_count = reminder_manifest["item_count"] if reminder_manifest else None
                run.reminders_absent_source_keys = reminder_manifest["absent_source_keys"] if reminder_manifest else []
                run.commit_request_digest = request_digest
                run.published_generation = generation
                run.state = SyncRun.State.COMMITTED
                run.committed_at = now
                run.save()
                response = {
                    "run_id": str(run.id),
                    "generation": generation,
                    "idempotent": False,
                    "events": "committed" if run.events_committable else "not_committed",
                    "reminders": "committed" if run.reminders_committable else "not_committed",
                }
                transaction.on_commit(lambda tenant_id=str(tenant.id): push_visibility_refresh(tenant_id))

    if deferred_error is not None:
        raise deferred_error
    return response


def _command_request_digest(command_type, payload, display_text, destination_name, destination_fingerprint, target_at):
    return _digest(
        {
            "command_type": command_type,
            "payload": payload,
            "display_text": display_text,
            "destination_name": destination_name,
            "destination_fingerprint": destination_fingerprint,
            "target_at": target_at.isoformat() if target_at else None,
        }
    )


def _walk_prohibited_payload(value) -> None:
    prohibited = {"attendees", "invitees", "recurrence", "recurrence_rule", "url", "urls"}
    if isinstance(value, dict):
        if prohibited.intersection(value):
            raise ProtocolError("unsupported_command_field")
        for child in value.values():
            _walk_prohibited_payload(child)
    elif isinstance(value, list):
        for child in value:
            _walk_prohibited_payload(child)
    elif isinstance(value, float):
        raise ProtocolError("floats_not_allowed")


def _command_date(value, code: str):
    parsed = parse_date(value) if isinstance(value, str) else None
    if parsed is None or parsed.isoformat() != value:
        raise ProtocolError(code)
    return parsed


def _command_datetime(value, code: str, *, aware: bool):
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or (parsed.tzinfo is not None) is not aware:
        raise ProtocolError(code)
    return parsed


def _validate_command_time(value) -> dict:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_command_time")
    kind = value.get("kind")
    if kind == "all_day":
        if set(value) != {"kind", "start_date", "end_date_exclusive"}:
            raise ProtocolError("invalid_command_time")
        start = _command_date(value["start_date"], "invalid_command_time")
        end = _command_date(value["end_date_exclusive"], "invalid_command_time")
    elif kind == "zoned":
        if set(value) != {"kind", "start_at", "end_at", "tz_id"}:
            raise ProtocolError("invalid_command_time")
        start = _command_datetime(value["start_at"], "invalid_command_time", aware=True)
        end = _command_datetime(value["end_at"], "invalid_command_time", aware=True)
        if not isinstance(value["tz_id"], str) or not 1 <= len(value["tz_id"]) <= 63:
            raise ProtocolError("invalid_command_time")
    elif kind == "floating":
        if set(value) != {"kind", "start_local", "end_local"}:
            raise ProtocolError("invalid_command_time")
        start = _command_datetime(value["start_local"], "invalid_command_time", aware=False)
        end = _command_datetime(value["end_local"], "invalid_command_time", aware=False)
    else:
        raise ProtocolError("invalid_command_time")
    if end <= start:
        raise ProtocolError("invalid_command_time")
    return dict(value)


def _validate_command_due(value) -> dict:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_command_due")
    kind = value.get("kind")
    if kind == "none":
        if set(value) != {"kind"}:
            raise ProtocolError("invalid_command_due")
    elif kind == "all_day":
        if set(value) != {"kind", "date"}:
            raise ProtocolError("invalid_command_due")
        _command_date(value["date"], "invalid_command_due")
    elif kind == "zoned":
        if set(value) != {"kind", "due_at", "tz_id"}:
            raise ProtocolError("invalid_command_due")
        _command_datetime(value["due_at"], "invalid_command_due", aware=True)
        if not isinstance(value["tz_id"], str) or not 1 <= len(value["tz_id"]) <= 63:
            raise ProtocolError("invalid_command_due")
    elif kind == "floating":
        if set(value) != {"kind", "due_local"}:
            raise ProtocolError("invalid_command_due")
        _command_datetime(value["due_local"], "invalid_command_due", aware=False)
    else:
        raise ProtocolError("invalid_command_due")
    return dict(value)


def _validate_command_alarm(value) -> dict:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_command_alarm")
    kind = value.get("kind")
    if kind == "absolute":
        if set(value) != {"kind", "trigger_at"}:
            raise ProtocolError("invalid_command_alarm")
        _command_datetime(value["trigger_at"], "invalid_command_alarm", aware=True)
    elif kind == "relative":
        if set(value) != {"kind", "offset_seconds"}:
            raise ProtocolError("invalid_command_alarm")
        offset = value["offset_seconds"]
        if isinstance(offset, bool) or not isinstance(offset, int) or not -604_800 <= offset <= 0:
            raise ProtocolError("invalid_command_alarm")
    else:
        raise ProtocolError("invalid_command_alarm")
    return dict(value)


def _validate_command_payload(payload, *, command_type=None) -> tuple[dict, int]:
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_command_payload")
    if set(payload) != {"items"}:
        raise ProtocolError("unsupported_command_field")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 5:
        raise ProtocolError("invalid_command_item_count")
    allowed = {
        "title",
        "location",
        "notes",
        "destination_name",
        "calendar_title",
        "list_title",
        "time",
        "due",
        "priority",
        "alarm",
    }
    cleaned_items = []
    for item in items:
        if not isinstance(item, dict) or not set(item).issubset(allowed):
            raise ProtocolError("unsupported_command_field")
        title = normalize_text(item.get("title"), 256)
        if not title.strip():
            raise ProtocolError("command_title_required")
        cleaned = dict(item)
        cleaned["title"] = title
        for key, limit in (
            ("location", 512),
            ("notes", 4000),
            ("destination_name", 256),
            ("calendar_title", 256),
            ("list_title", 256),
        ):
            if key in cleaned:
                cleaned[key] = normalize_text(cleaned[key], limit)
        if "priority" in cleaned:
            priority = cleaned["priority"]
            if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 9:
                raise ProtocolError("invalid_command_priority")
        for key in ("time", "due", "alarm"):
            if key in cleaned and not isinstance(cleaned[key], dict):
                raise ProtocolError(f"invalid_command_{key}")
        if command_type == DeviceCommand.CommandType.CALENDAR_CREATE:
            if "time" not in cleaned or {"due", "priority", "list_title"}.intersection(cleaned):
                raise ProtocolError("invalid_calendar_command_item")
        elif command_type == DeviceCommand.CommandType.REMINDER_CREATE:
            if {"time", "calendar_title"}.intersection(cleaned):
                raise ProtocolError("invalid_reminder_command_item")
            cleaned.setdefault("due", {"kind": "none"})
        if "time" in cleaned:
            cleaned["time"] = _validate_command_time(cleaned["time"])
        if "due" in cleaned:
            cleaned["due"] = _validate_command_due(cleaned["due"])
        if "alarm" in cleaned:
            cleaned["alarm"] = _validate_command_alarm(cleaned["alarm"])
        cleaned_items.append(cleaned)
    payload = {"items": cleaned_items}
    _walk_prohibited_payload(payload)
    if len(canonical_json_bytes(payload)) > COMMAND_PAYLOAD_BYTES:
        raise ProtocolError("command_payload_too_large")
    return payload, len(items)


def create_device_command(
    tenant,
    *,
    command_id=None,
    request_id,
    command_type,
    payload,
    display_text="",
    destination_name="",
    destination_fingerprint="",
    target_at=None,
) -> tuple[DeviceCommand, bool]:
    """B1's dormant runtime-create seam: idempotent, placeholdered, quota-atomic."""

    if not datebook_delivery_ready(tenant):
        raise ProtocolError("datebook_disabled", 409)
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id.strip()) > 128:
        raise ProtocolError("invalid_request_id")
    if command_type not in DeviceCommand.CommandType.values:
        raise ProtocolError("invalid_command_type")
    # Preserve B1's internal seam for existing callers; the B2a runtime entry
    # performs the stricter command-type/tagged-time validation before gating.
    payload, item_count = _validate_command_payload(payload)
    display_text = normalize_text(display_text, 512)
    destination_name = normalize_text(destination_name, 256)
    if not isinstance(destination_fingerprint, str) or len(destination_fingerprint) > 64:
        raise ProtocolError("invalid_destination_fingerprint")
    if target_at is not None and (not isinstance(target_at, datetime) or target_at.tzinfo is None):
        raise ProtocolError("invalid_target_at")
    request_id = request_id.strip()
    if command_id is not None:
        try:
            command_id = uuid.UUID(str(command_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProtocolError("invalid_command_id") from exc
    request_digest = _command_request_digest(
        command_type,
        payload,
        display_text,
        destination_name,
        destination_fingerprint,
        target_at,
    )

    now = timezone.now()
    tz = tenant_tz(tenant)
    local_day = now.astimezone(tz).date()
    day_start = datetime.combine(local_day, time.min, tzinfo=tz).astimezone(UTC)
    day_end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)

    with transaction.atomic():
        locked_tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
        if not datebook_delivery_ready(locked_tenant):
            raise ProtocolError("datebook_disabled", 409)
        existing = DeviceCommand.objects.filter(tenant=locked_tenant, request_id=request_id).first()
        if existing is not None:
            if existing.request_digest != request_digest or (command_id is not None and existing.id != command_id):
                raise ProtocolError("request_id_conflict", 409)
            return existing, False
        gateway = _locked_active_gateway(locked_tenant)
        used = (
            DeviceCommand.objects.filter(
                tenant=locked_tenant,
                created_at__gte=day_start,
                created_at__lt=day_end,
            ).aggregate(total=Sum("item_count"))["total"]
            or 0
        )
        if used + item_count > COMMAND_DAILY_ITEM_CAP:
            raise ProtocolError("daily_command_cap", 429)
        authored, receipts = author_store_fields(
            locked_tenant,
            {
                "payload": payload,
                "display_text": display_text,
                "destination_name": destination_name,
                "result_display": "",
            },
            model_label="datebook.DeviceCommand",
            seam="datebook.runtime.command.create",
            writer="runtime",
        )
        try:
            command = DeviceCommand.objects.create(
                id=command_id or uuid.uuid4(),
                tenant=locked_tenant,
                request_id=request_id,
                request_digest=request_digest,
                command_type=command_type,
                item_count=item_count,
                target_installation_id=gateway.installation_id,
                target_gateway_epoch=gateway.gateway_epoch,
                destination_fingerprint=destination_fingerprint,
                destination_name=authored["destination_name"],
                display_text=authored["display_text"],
                payload=authored["payload"],
                pii_receipts=receipts,
                expires_at=now + timedelta(hours=COMMAND_TTL_HOURS),
                target_at=target_at,
            )
        except IntegrityError:
            command = DeviceCommand.objects.filter(tenant=locked_tenant, request_id=request_id).first()
            if command is None:
                raise ProtocolError("command_id_conflict", 409) from None
            if command.request_digest != request_digest:
                raise ProtocolError("request_id_conflict", 409) from None
            if command_id is not None and command.id != command_id:
                raise ProtocolError("request_id_conflict", 409) from None
            return command, False
        return command, True


def datebook_command_generation(tenant) -> int:
    """Monotonic tenant hint derived from the latest command update epoch."""

    latest = DeviceCommand.objects.filter(tenant=tenant).aggregate(value=Max("updated_at"))["value"]
    return int(latest.timestamp() * 1_000_000) if latest is not None else 0


def _command_is_past(command, now) -> bool:
    return command.expires_at <= now or (command.target_at is not None and command.target_at <= now)


def sweep_device_commands(*, tenant=None, now=None) -> dict[str, int]:
    from apps.actions.models import ActionAuditOutcome
    from apps.actions.services import record_datebook_command_transition

    now = now or timezone.now()
    counts = {"requeued": 0, "expired": 0, "ambiguous": 0}
    tenant_filter = {"tenant": tenant} if tenant is not None else {}
    with transaction.atomic():
        leased = list(
            DeviceCommand.objects.select_for_update().filter(
                **tenant_filter,
                state=DeviceCommand.State.LEASED,
                lease_expires_at__lte=now,
                started_at__isnull=True,
            )
        )
        for command in leased:
            if _command_is_past(command, now):
                command.state = DeviceCommand.State.EXPIRED
                command.resolved_at = now
                counts["expired"] += 1
            else:
                command.state = DeviceCommand.State.PENDING
                counts["requeued"] += 1
            command.lease_token = None
            command.lease_expires_at = None
            command.save(update_fields=["state", "resolved_at", "lease_token", "lease_expires_at", "updated_at"])
            if command.state == DeviceCommand.State.EXPIRED:
                record_datebook_command_transition(command, ActionAuditOutcome.COMMAND_EXPIRED)
        expiring = list(
            DeviceCommand.objects.select_for_update().filter(
                Q(expires_at__lte=now) | Q(target_at__lte=now),
                **tenant_filter,
                state=DeviceCommand.State.PENDING,
                started_at__isnull=True,
            )
        )
        for command in expiring:
            command.state = DeviceCommand.State.EXPIRED
            command.resolved_at = now
            command.save(update_fields=["state", "resolved_at", "updated_at"])
            counts["expired"] += 1
            record_datebook_command_transition(command, ActionAuditOutcome.COMMAND_EXPIRED)
        ambiguous_commands = list(
            DeviceCommand.objects.select_for_update().filter(
                **tenant_filter,
                state=DeviceCommand.State.EXECUTING,
                execution_deadline_at__lte=now,
            )
        )
        for command in ambiguous_commands:
            command.state = DeviceCommand.State.AMBIGUOUS
            command.execution_status = DeviceCommand.ExecutionStatus.AMBIGUOUS
            command.resolved_at = now
            command.save(update_fields=["state", "execution_status", "resolved_at", "updated_at"])
            counts["ambiguous"] += 1
            record_datebook_command_transition(command, ActionAuditOutcome.AMBIGUOUS)
    return counts


def claim_device_command(tenant, *, installation_id, gateway_epoch) -> DeviceCommand | None:
    installation_id = _installation_id(installation_id)
    gateway_epoch = _positive_epoch(gateway_epoch)
    sweep_device_commands(tenant=tenant)
    now = timezone.now()
    token = uuid.uuid4()
    with transaction.atomic():
        gateway = _locked_active_gateway(tenant)
        _assert_gateway(gateway, installation_id=installation_id, gateway_epoch=gateway_epoch)
        candidate_id = (
            DeviceCommand.objects.filter(
                tenant=tenant,
                state=DeviceCommand.State.PENDING,
                target_installation_id=installation_id,
                target_gateway_epoch=gateway_epoch,
                expires_at__gt=now,
            )
            .filter(Q(target_at__isnull=True) | Q(target_at__gt=now))
            .order_by("created_at", "id")
            .values_list("id", flat=True)
            .first()
        )
        if candidate_id is None:
            return None
        won = DeviceCommand.objects.filter(
            id=candidate_id,
            state=DeviceCommand.State.PENDING,
            lease_token__isnull=True,
        ).update(
            state=DeviceCommand.State.LEASED,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=COMMAND_LEASE_SECONDS),
            updated_at=now,
        )
        if not won:
            return None
        return DeviceCommand.objects.get(id=candidate_id)


def start_device_command(
    tenant,
    *,
    command_id,
    lease_token,
    installation_id,
    gateway_epoch,
    destination_fingerprint,
) -> tuple[DeviceCommand, bool]:
    installation_id = _installation_id(installation_id)
    gateway_epoch = _positive_epoch(gateway_epoch)
    try:
        lease_token = uuid.UUID(str(lease_token))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProtocolError("invalid_lease_token", 409) from exc
    now = timezone.now()
    deferred_error = None
    result = None
    with transaction.atomic():
        gateway = _locked_active_gateway(tenant)
        _assert_gateway(gateway, installation_id=installation_id, gateway_epoch=gateway_epoch)
        try:
            command = DeviceCommand.objects.select_for_update().get(id=command_id, tenant=tenant)
        except (DeviceCommand.DoesNotExist, ValueError, TypeError) as exc:
            raise ProtocolError("command_not_found", 404) from exc
        if (
            command.target_installation_id != gateway.installation_id
            or command.target_gateway_epoch != gateway.gateway_epoch
        ):
            raise ProtocolError("stale_command_target", 409)
        if command.lease_token != lease_token:
            raise ProtocolError("invalid_lease_token", 409)
        if command.state == DeviceCommand.State.EXECUTING:
            return command, True
        if command.state != DeviceCommand.State.LEASED:
            raise ProtocolError("command_not_leased", 409)
        if _command_is_past(command, now):
            command.state = DeviceCommand.State.EXPIRED
            command.lease_token = None
            command.lease_expires_at = None
            command.resolved_at = now
            command.save(update_fields=["state", "lease_token", "lease_expires_at", "resolved_at", "updated_at"])
            from apps.actions.models import ActionAuditOutcome
            from apps.actions.services import record_datebook_command_transition

            record_datebook_command_transition(command, ActionAuditOutcome.COMMAND_EXPIRED)
            deferred_error = ProtocolError("command_expired", 409)
        elif command.lease_expires_at <= now:
            command.state = DeviceCommand.State.PENDING
            command.lease_token = None
            command.lease_expires_at = None
            command.save(update_fields=["state", "lease_token", "lease_expires_at", "updated_at"])
            deferred_error = ProtocolError("lease_expired", 409, {"requeueable": True})
        elif destination_fingerprint != command.destination_fingerprint:
            deferred_error = ProtocolError("destination_changed", 409)
        else:
            command.state = DeviceCommand.State.EXECUTING
            command.started_at = now
            command.execution_deadline_at = now + timedelta(seconds=COMMAND_EXECUTION_TIMEOUT_SECONDS)
            command.execution_status = DeviceCommand.ExecutionStatus.EXECUTING
            command.save(
                update_fields=[
                    "state",
                    "started_at",
                    "execution_deadline_at",
                    "execution_status",
                    "updated_at",
                ]
            )
            result = command
    if deferred_error:
        raise deferred_error
    return result, False


def _validate_result_identifiers(value) -> dict:
    allowed = {
        "item_index",
        "calendar_item_id",
        "reminder_id",
        "external_id",
        "series_id",
        "calendar_identifier",
        "list_identifier",
        "occurrence_key",
    }

    def clean_record(record) -> dict:
        if not isinstance(record, dict) or not set(record).issubset(allowed):
            raise ProtocolError("invalid_result_identifiers")
        cleaned = {}
        for key, raw in record.items():
            if key == "item_index":
                if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 4:
                    raise ProtocolError("invalid_result_identifiers")
                cleaned[key] = raw
            else:
                if not isinstance(raw, str) or not raw or len(raw) > 255:
                    raise ProtocolError("invalid_result_identifiers")
                cleaned[key] = raw
        return cleaned

    if not isinstance(value, dict):
        raise ProtocolError("invalid_result_identifiers")
    if "items" in value:
        if set(value) != {"items"} or not isinstance(value["items"], list) or len(value["items"]) > 5:
            raise ProtocolError("invalid_result_identifiers")
        return {"items": [clean_record(record) for record in value["items"]]}
    return clean_record(value)


def finish_device_command(
    tenant,
    *,
    command_id,
    lease_token,
    result_id,
    execution_status,
    mirror_status,
    safe_error,
    result_identifiers,
    result_display,
    journaled_at,
) -> tuple[DeviceCommand, bool]:
    try:
        lease_token = uuid.UUID(str(lease_token))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProtocolError("invalid_lease_token", 409) from exc
    if not isinstance(result_id, str) or not result_id.strip() or len(result_id.strip()) > 64:
        raise ProtocolError("invalid_result_id")
    result_id = result_id.strip()
    if execution_status not in [DeviceCommand.State.EXECUTED, DeviceCommand.State.FAILED]:
        raise ProtocolError("invalid_execution_status")
    if mirror_status not in DeviceCommand.MirrorStatus.values:
        raise ProtocolError("invalid_mirror_status")
    if safe_error not in DeviceCommand.SafeError.values:
        raise ProtocolError("invalid_safe_error")
    if execution_status == DeviceCommand.State.FAILED and not safe_error:
        raise ProtocolError("safe_error_required")
    if execution_status == DeviceCommand.State.EXECUTED and safe_error:
        raise ProtocolError("unexpected_safe_error")
    result_identifiers = _validate_result_identifiers(result_identifiers)
    if not isinstance(journaled_at, datetime) or journaled_at.tzinfo is None:
        raise ProtocolError("invalid_journaled_at")
    journaled_at = journaled_at.astimezone(UTC).replace(microsecond=0)
    result_display = normalize_text(result_display, 512)
    result_request_digest = _digest(
        {
            "result_id": result_id,
            "execution_status": execution_status,
            "mirror_status": mirror_status,
            "safe_error": safe_error,
            "result_identifiers": result_identifiers,
            "result_display": result_display,
            "journaled_at": journaled_at.isoformat(),
        }
    )
    now = timezone.now()
    with transaction.atomic():
        locked_tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
        try:
            command = DeviceCommand.objects.select_for_update().get(id=command_id, tenant=locked_tenant)
        except (DeviceCommand.DoesNotExist, ValueError, TypeError) as exc:
            raise ProtocolError("command_not_found", 404) from exc
        if command.lease_token != lease_token:
            raise ProtocolError("invalid_lease_token", 409)
        if command.state in [DeviceCommand.State.EXECUTED, DeviceCommand.State.FAILED]:
            if command.result_request_digest != result_request_digest:
                raise ProtocolError("result_conflict", 409)
            return command, True
        if command.state != DeviceCommand.State.EXECUTING:
            raise ProtocolError("command_not_executing", 409)
        authored, receipts = author_store_fields(
            locked_tenant,
            {"result_display": result_display},
            model_label="datebook.DeviceCommand",
            seam="datebook.owner.eventkit.command.result",
            writer="owner",
            receipts=command.pii_receipts,
        )
        command.state = execution_status
        command.execution_status = (
            DeviceCommand.ExecutionStatus.SUCCEEDED
            if execution_status == DeviceCommand.State.EXECUTED
            else DeviceCommand.ExecutionStatus.FAILED
        )
        command.mirror_status = mirror_status
        command.safe_error = safe_error
        command.result_id = result_id
        command.result_request_digest = result_request_digest
        command.result_identifiers = result_identifiers
        command.result_display = authored["result_display"]
        command.journaled_at = journaled_at
        command.pii_receipts = receipts
        command.resolved_at = now
        command.save()
        from apps.actions.models import ActionAuditOutcome
        from apps.actions.services import record_datebook_command_transition

        outcome = (
            ActionAuditOutcome.EXECUTED if command.state == DeviceCommand.State.EXECUTED else ActionAuditOutcome.FAILED
        )
        record_datebook_command_transition(command, outcome, detail_code=command.safe_error)
        return command, False
