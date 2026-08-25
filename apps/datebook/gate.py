"""Datebook-specific orchestration on top of the shared action review gate."""

from __future__ import annotations

import logging
import unicodedata
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta

from django.conf import settings
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.actions.models import ActionAuditOutcome, ActionStatus, ActionType, PendingAction
from apps.actions.origin import OriginStamp
from apps.actions.services import record_action_audit
from apps.common.eval_sink import suppresses_real_transport
from apps.pii.store_authoring import author_store_fields, owner_store_representation
from apps.tenants.models import Tenant

from .hashing import HASH_RE, canonical_json_bytes
from .models import DatebookDestinationDefault, DatebookGateway, DeviceCommand
from .services import ProtocolError

DATEBOOK_ACTION_TYPES = frozenset({ActionType.CALENDAR_CREATE, ActionType.REMINDER_CREATE})
DATEBOOK_GATE_REVIEW_WINDOW = timedelta(hours=24)
DATEBOOK_GATE_REVIEW_WINDOW_SECONDS = int(DATEBOOK_GATE_REVIEW_WINDOW.total_seconds())
DATEBOOK_GATE_APPROVAL_FLOOR = timedelta(minutes=5)
DATEBOOK_DUPLICATE_WINDOW = timedelta(minutes=2)
DATEBOOK_CREATE_LOCK_TIMEOUT_MS = 2_000
DATEBOOK_CREATE_STATEMENT_TIMEOUT_MS = 5_000
DATEBOOK_CREATE_RETRY_STATE = "request_temporarily_unavailable"
DATEBOOK_CREATE_RETRY_GUIDANCE = (
    "Nothing was created yet. Calendar & Reminders is temporarily busy; retry this request later."
)
DEVICE_DEFAULT_DESTINATION = "device_default"
UNDELIVERABLE_REASON = "needs_app_update_or_linked_chat"
UNDELIVERABLE_MESSAGE = "This calendar request needs the app update or a linked chat channel to approve."
STALE_REVIEW_REASON = "stale_review"
STALE_REVIEW_MESSAGE = "The 24-hour review window expired. Nothing was queued or created."
TARGET_PASSED_REASON = "target_passed"
TARGET_PASSED_MESSAGE = "The item's time passed before approval. Nothing was queued or created."
STALE_REVIEW_REASONS = frozenset({STALE_REVIEW_REASON, TARGET_PASSED_REASON})

logger = logging.getLogger(__name__)


def _bounded_timeout_setting(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, 1), 60_000)


@contextmanager
def datebook_create_db_budget():
    """Bound every database statement and row-lock wait in request-create."""

    with transaction.atomic():
        if connection.vendor == "postgresql":
            lock_ms = _bounded_timeout_setting(
                "DATEBOOK_REQUEST_CREATE_LOCK_TIMEOUT_MS",
                DATEBOOK_CREATE_LOCK_TIMEOUT_MS,
            )
            statement_ms = _bounded_timeout_setting(
                "DATEBOOK_REQUEST_CREATE_STATEMENT_TIMEOUT_MS",
                DATEBOOK_CREATE_STATEMENT_TIMEOUT_MS,
            )
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = %s", [f"{lock_ms}ms"])
                cursor.execute("SET LOCAL statement_timeout = %s", [f"{statement_ms}ms"])
        yield


def datebook_create_retry_data() -> dict:
    return {
        "state": DATEBOOK_CREATE_RETRY_STATE,
        "retriable": True,
        "created": False,
        "guidance": DATEBOOK_CREATE_RETRY_GUIDANCE,
    }


def _create_db_unavailable(tenant: Tenant, exc: OperationalError) -> ProtocolError:
    logger.warning(
        "datebook request-create database budget exhausted tenant=%s error_type=%s",
        tenant.id,
        type(exc).__name__,
    )
    return ProtocolError(
        DATEBOOK_CREATE_RETRY_STATE,
        503,
        {key: value for key, value in datebook_create_retry_data().items() if key != "state"},
    )


def is_datebook_action_type(action_type: str) -> bool:
    return action_type in DATEBOOK_ACTION_TYPES


def earliest_datebook_target_at(tenant: Tenant, command_type: str, payload: dict) -> datetime | None:
    """Return the earliest absolute due/start time represented by a batch."""

    from apps.common.tenant_tz import tenant_tz

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    candidates = []
    key = "time" if command_type == DeviceCommand.CommandType.CALENDAR_CREATE else "due"
    for item in items:
        if not isinstance(item, dict):
            continue
        tagged = item.get(key, {"kind": "none"})
        if not isinstance(tagged, dict):
            continue
        kind = tagged.get("kind")
        if kind == "all_day":
            raw = tagged.get("start_date") or tagged.get("date")
            parsed = parse_date(raw) if isinstance(raw, str) else None
            if parsed is not None:
                candidates.append(datetime.combine(parsed, time.max, tzinfo=tenant_tz(tenant)))
        elif kind == "zoned":
            raw = tagged.get("start_at") or tagged.get("due_at")
            parsed = parse_datetime(raw) if isinstance(raw, str) else None
            if parsed is not None and timezone.is_aware(parsed):
                candidates.append(parsed)
        elif kind == "floating":
            raw = tagged.get("start_local") or tagged.get("due_local")
            parsed = parse_datetime(raw) if isinstance(raw, str) else None
            if parsed is not None and timezone.is_naive(parsed):
                candidates.append(parsed.replace(tzinfo=tenant_tz(tenant)))
    return min(candidates) if candidates else None


def _datebook_gate_expires_at(*, now: datetime, target_at: datetime | None) -> datetime:
    deadline = now + DATEBOOK_GATE_REVIEW_WINDOW
    if target_at is not None:
        deadline = min(deadline, target_at)
    return max(now + DATEBOOK_GATE_APPROVAL_FLOOR, deadline)


def _stale_review_message(action: PendingAction) -> str:
    if action.resolution_code == TARGET_PASSED_REASON:
        return TARGET_PASSED_MESSAGE
    return STALE_REVIEW_MESSAGE


def _delivery_facts(action: PendingAction) -> tuple[str, str]:
    surface = action.platform_channel or action.originating_channel
    if surface not in {"app", "telegram", "line"}:
        surface = "app"
    delivery_state = action.delivery_state
    if not delivery_state:
        if surface == "app":
            delivery_state = "available"
        elif action.platform_message_id and not (
            surface == "line" and action.platform_message_id.startswith("line-push-")
        ):
            delivery_state = "sent"
        elif surface == "line" and action.platform_message_id.startswith("line-push-"):
            # Pre-approval-UX LINE rows used a synthetic success marker rather
            # than a platform message id. Preserve acceptance without ever
            # upgrading that legacy marker into a "sent" delivery claim.
            delivery_state = "accepted"
        else:
            delivery_state = "unconfirmed"
    return surface, delivery_state


def _guidance(action: PendingAction, *, state: str, surface: str, delivery_state: str) -> str:
    if state == "approval_pending":
        if surface == "app":
            return "Waiting for your approval; the approval is in this conversation. Review it within 24 hours."
        if delivery_state == "sent":
            return f"Waiting for your approval. I sent the approval to {surface.title()}; review it within 24 hours."
        return (
            f"Waiting for your approval in {surface.title()}, but message delivery was not confirmed. "
            "Review it within 24 hours."
        )
    if state == STALE_REVIEW_REASON:
        return _stale_review_message(action)
    if state == "undeliverable":
        return UNDELIVERABLE_MESSAGE
    if state == "denied":
        return "The user denied the Calendar & Reminders request; nothing was queued."
    if state == "approved_queued":
        return "Approved and queued for up to 72 hours. The device has not yet confirmed creation."
    if action.status == ActionStatus.APPROVED:
        return f"{state}: the approved request was not confirmed as created."
    return f"{state}: the request was not confirmed as created."


def _action_state(action: PendingAction) -> dict:
    surface, delivery_state = _delivery_facts(action)
    base = {
        "action_id": action.id,
        "command_id": str(action.datebook_command_id),
        "approval_surface": surface,
        "delivery_state": delivery_state,
    }
    if action.status == ActionStatus.PENDING:
        state = "approval_pending"
        return {
            **base,
            "state": state,
            "expires_at": action.expires_at.isoformat(),
            "guidance": _guidance(action, state=state, surface=surface, delivery_state=delivery_state),
        }
    if action.status == ActionStatus.DENIED:
        state = "denied"
        return {
            **base,
            "state": state,
            "guidance": _guidance(action, state=state, surface=surface, delivery_state=delivery_state),
        }
    if action.status == ActionStatus.EXPIRED:
        if action.resolution_code == UNDELIVERABLE_REASON:
            return {
                **base,
                "state": "undeliverable",
                "reason": UNDELIVERABLE_REASON,
                "message": UNDELIVERABLE_MESSAGE,
                "guidance": UNDELIVERABLE_MESSAGE,
            }
        state = STALE_REVIEW_REASON if action.resolution_code in STALE_REVIEW_REASONS else "expired"
        data = {
            **base,
            "state": state,
            "guidance": _guidance(action, state=state, surface=surface, delivery_state=delivery_state),
        }
        if state == STALE_REVIEW_REASON:
            data["message"] = _stale_review_message(action)
        return data
    if action.resolution_code and action.resolution_code != ActionAuditOutcome.QUEUED:
        state = action.resolution_code
        return {
            **base,
            "state": state,
            "guidance": _guidance(action, state=state, surface=surface, delivery_state=delivery_state),
        }
    state = "approved_queued"
    return {
        **base,
        "state": state,
        "guidance": _guidance(action, state=state, surface=surface, delivery_state=delivery_state),
    }


def datebook_action_state(action: PendingAction) -> dict:
    with datebook_create_db_budget():
        action = PendingAction.objects.select_for_update().get(pk=action.pk)
        if action.is_expired:
            return expire_datebook_action(action)
        return _action_state(action)


def _entity_type(action_type: str) -> str:
    if action_type == ActionType.CALENDAR_CREATE:
        return DatebookDestinationDefault.EntityType.CALENDAR
    if action_type == ActionType.REMINDER_CREATE:
        return DatebookDestinationDefault.EntityType.REMINDER
    raise ValueError("invalid datebook action type")


def _clean_destination_name(value) -> str:
    if not isinstance(value, str):
        raise ProtocolError("invalid_destination_name")
    value = value.strip()
    if len(value) > 256:
        raise ProtocolError("invalid_destination_name")
    return value


def _clean_fingerprint(value, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ProtocolError("invalid_destination_fingerprint")
    value = value.strip().lower()
    if value and HASH_RE.fullmatch(value) is None:
        raise ProtocolError("invalid_destination_fingerprint")
    if required and not value:
        raise ProtocolError("destination_fingerprint_required")
    return value


def _resolve_requested_destination(tenant: Tenant, action_type: str, command_payload: dict) -> dict:
    fields = deepcopy(command_payload)
    payload = deepcopy(fields.get("payload"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ProtocolError("invalid_command_payload")

    command_type = fields.get("command_type")
    item_key = "calendar_title" if command_type == DeviceCommand.CommandType.CALENDAR_CREATE else "list_title"
    item_names: set[str] = set()
    cleaned_items = []
    for item in payload["items"]:
        cleaned = dict(item)
        for key in ("destination_name", item_key):
            if key in cleaned:
                name = _clean_destination_name(cleaned.pop(key))
                if name:
                    item_names.add(name)
        cleaned_items.append(cleaned)
    if len(item_names) > 1:
        raise ProtocolError("conflicting_destination")

    explicit_name = _clean_destination_name(fields.get("destination_name", ""))
    item_name = next(iter(item_names), "")
    if explicit_name and item_name and explicit_name != item_name:
        raise ProtocolError("conflicting_destination")
    explicit_name = explicit_name or item_name
    explicit_fingerprint = _clean_fingerprint(fields.get("destination_fingerprint", ""))
    if explicit_fingerprint and not explicit_name:
        raise ProtocolError("destination_name_required")

    payload["items"] = cleaned_items
    fields["payload"] = payload
    entity_type = _entity_type(action_type)
    if explicit_name:
        requested = {
            "kind": "explicit",
            "name": explicit_name,
            "fingerprint": explicit_fingerprint,
        }
    else:
        gateway = DatebookGateway.objects.filter(tenant=tenant, status=DatebookGateway.Status.ACTIVE).first()
        default = (
            DatebookDestinationDefault.objects.select_for_update()
            .filter(tenant=tenant, entity_type=entity_type)
            .first()
        )
        if default is not None and (
            gateway is None
            or default.target_installation_id != gateway.installation_id
            or default.gateway_epoch != gateway.gateway_epoch
            or HASH_RE.fullmatch(default.fingerprint) is None
        ):
            default.delete()
            default = None
        if default is not None:
            represented = owner_store_representation(
                default,
                tenant,
                {"name": default.name},
                model_label="datebook.DatebookDestinationDefault",
            )
            requested = {
                "kind": "tenant_default",
                "name": represented["name"],
                "fingerprint": default.fingerprint,
            }
        else:
            requested = {"kind": DEVICE_DEFAULT_DESTINATION, "name": "", "fingerprint": ""}

    fields["destination_kind"] = requested["kind"]
    fields["destination_name"] = requested["name"]
    fields["destination_fingerprint"] = requested["fingerprint"]
    fields["requested_destination"] = requested
    return fields


def _author_pending_datebook_fields(
    tenant: Tenant,
    *,
    action_payload: dict,
    display_summary: str,
    seam: str,
    writer: str,
    defer_detection: bool = False,
) -> tuple[dict, dict]:
    shielded_payload = deepcopy(action_payload)
    recurrences = {}
    payload = shielded_payload.get("payload")
    items = payload.get("items") if isinstance(payload, dict) else None
    if isinstance(items, list):
        for index, item in enumerate(items):
            if isinstance(item, dict) and "recurrence" in item:
                # Protocol enums and ISO dates are not user-authored text. Keep
                # them outside placeholder authoring so the device wire object
                # remains exact even when an enum collides with an entity name.
                recurrences[index] = deepcopy(item["recurrence"])
                item["recurrence"] = None

    authored, receipts = author_store_fields(
        tenant,
        {
            "action_payload": shielded_payload,
            "display_summary": display_summary,
        },
        model_label="actions.PendingAction",
        seam=seam,
        writer=writer,
        defer_detection=defer_detection,
    )
    authored_items = authored["action_payload"]["payload"]["items"]
    for index, recurrence in recurrences.items():
        authored_items[index]["recurrence"] = recurrence
    return authored, receipts


def _rehydrated_command_fields(action: PendingAction) -> dict:
    represented = owner_store_representation(
        action,
        action.tenant,
        {"action_payload": action.action_payload},
        model_label="actions.PendingAction",
    )
    represented_payload = represented["action_payload"]
    if not isinstance(represented_payload, dict):
        raise ValueError("datebook gate payload is not an object")
    return dict(represented_payload)


def _normalized_title(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _target_minute(value) -> str | None:
    if value in (None, ""):
        return ""
    parsed = value if isinstance(value, datetime) else parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or timezone.is_naive(parsed):
        return None
    return parsed.astimezone(UTC).replace(second=0, microsecond=0).isoformat()


def _datebook_expiry_reason(action: PendingAction) -> str:
    try:
        target_at = _rehydrated_command_fields(action).get("target_at")
    except Exception:
        logger.warning(
            "datebook expiry could not read target tenant=%s action=%s",
            action.tenant_id,
            action.id,
            exc_info=True,
        )
        return STALE_REVIEW_REASON
    target_at = (
        target_at
        if isinstance(target_at, datetime)
        else parse_datetime(target_at)
        if isinstance(target_at, str)
        else None
    )
    if target_at is not None and timezone.is_aware(target_at) and target_at <= action.expires_at:
        return TARGET_PASSED_REASON
    return STALE_REVIEW_REASON


def _logical_request_signature(command_payload: dict) -> tuple | None:
    if not isinstance(command_payload, dict):
        return None
    payload = command_payload.get("payload")
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return None
    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        recurrence_signature = b""
        if "recurrence" in item:
            recurrence = deepcopy(item["recurrence"])
            if isinstance(recurrence, dict):
                recurrence.setdefault("interval", 1)
            recurrence_signature = canonical_json_bytes(recurrence)
        normalized_items.append((_normalized_title(item.get("title")), recurrence_signature))
    titles = tuple(sorted(title for title, _recurrence in normalized_items))
    if len(normalized_items) != len(items) or any(not title for title in titles):
        return None
    target_minute = _target_minute(command_payload.get("target_at"))
    if target_minute is None:
        return None
    command_type = command_payload.get("command_type")
    if not isinstance(command_type, str) or not command_type:
        return None
    if any(recurrence for _title, recurrence in normalized_items):
        return command_type, tuple(sorted(normalized_items)), target_minute
    return command_type, titles, target_minute


def _recent_duplicate_action(
    tenant: Tenant,
    *,
    action_type: str,
    command_payload: dict,
) -> PendingAction | None:
    """Find an identical recent pending gate after the tenant row is locked."""

    signature = _logical_request_signature(command_payload)
    if signature is None:
        return None
    recent = (
        PendingAction.objects.filter(
            tenant=tenant,
            action_type=action_type,
            status=ActionStatus.PENDING,
            created_at__gte=timezone.now() - DATEBOOK_DUPLICATE_WINDOW,
        )
        .select_related("tenant")
        .order_by("-created_at")[:20]
    )
    for candidate in recent:
        try:
            candidate_signature = _logical_request_signature(_rehydrated_command_fields(candidate))
        except Exception:
            logger.warning(
                "datebook duplicate guard skipped unreadable action tenant=%s action=%s",
                tenant.id,
                candidate.id,
                exc_info=True,
            )
            continue
        if candidate_signature == signature:
            return candidate
    return None


def _approved_target(fields: dict, destination_override, *, set_default: bool) -> tuple[dict, dict]:
    requested = fields.get("requested_destination")
    if not isinstance(requested, dict):
        requested = {
            "kind": fields.get("destination_kind") or "legacy",
            "name": fields.get("destination_name", ""),
            "fingerprint": fields.get("destination_fingerprint", ""),
        }
    requested = deepcopy(requested)
    if destination_override is None:
        approved = deepcopy(requested)
    else:
        if not isinstance(destination_override, dict) or set(destination_override) != {"name", "fingerprint"}:
            raise ProtocolError("invalid_destination_override")
        approved = {
            "kind": "override",
            "name": _clean_destination_name(destination_override.get("name")),
            "fingerprint": _clean_fingerprint(destination_override.get("fingerprint"), required=True),
        }
        if not approved["name"]:
            raise ProtocolError("invalid_destination_override")
    if set_default and (not approved.get("name") or not approved.get("fingerprint")):
        raise ProtocolError("default_requires_fingerprinted_destination")
    return requested, approved


def _schedule_gate_changed(action: PendingAction) -> None:
    from .notify import dispatch_datebook_gate_changed

    tenant_id = action.tenant_id
    transaction.on_commit(lambda tenant_id=tenant_id: dispatch_datebook_gate_changed(tenant_id))


def _persist_destination_default(action: PendingAction, approved: dict, command: DeviceCommand) -> tuple[str, str]:
    entity_type = _entity_type(action.action_type)
    current = (
        DatebookDestinationDefault.objects.select_for_update()
        .filter(tenant=action.tenant, entity_type=entity_type)
        .first()
    )
    old_fingerprint = current.fingerprint if current is not None else ""
    authored, receipts = author_store_fields(
        action.tenant,
        {"name": approved["name"]},
        model_label="datebook.DatebookDestinationDefault",
        seam="datebook.owner.destination_default",
        writer="owner",
    )
    if current is None:
        DatebookDestinationDefault.objects.create(
            tenant=action.tenant,
            entity_type=entity_type,
            name=authored["name"],
            fingerprint=approved["fingerprint"],
            target_installation_id=command.target_installation_id,
            gateway_epoch=command.target_gateway_epoch,
            pii_receipts=receipts,
        )
    else:
        current.name = authored["name"]
        current.fingerprint = approved["fingerprint"]
        current.target_installation_id = command.target_installation_id
        current.gateway_epoch = command.target_gateway_epoch
        current.pii_receipts = receipts
        current.save(
            update_fields=[
                "name",
                "fingerprint",
                "target_installation_id",
                "gateway_epoch",
                "pii_receipts",
                "updated_at",
            ]
        )
    return old_fingerprint, approved["fingerprint"]


def approve_datebook_action(
    action: PendingAction,
    *,
    responded_at,
    destination_override=None,
    set_default: bool = False,
) -> dict:
    """Persist approval, command creation, links, and audit rows atomically."""

    from .services import create_device_command

    if not isinstance(set_default, bool):
        raise ProtocolError("invalid_set_default")

    fields = _rehydrated_command_fields(action)
    requested, approved = _approved_target(fields, destination_override, set_default=set_default)
    fields["requested_destination"] = requested
    fields["approved_destination"] = approved
    fields["destination_kind"] = approved["kind"]
    fields["destination_name"] = approved["name"]
    fields["destination_fingerprint"] = approved["fingerprint"]
    represented_summary = owner_store_representation(
        action,
        action.tenant,
        {"display_summary": action.display_summary},
        model_label="actions.PendingAction",
    )["display_summary"]
    authored, receipts = _author_pending_datebook_fields(
        action.tenant,
        action_payload=fields,
        display_summary=represented_summary,
        seam="datebook.owner.review.approved_target",
        writer="owner",
    )

    action.status = ActionStatus.APPROVED
    action.responded_at = responded_at
    action.action_payload = authored["action_payload"]
    action.display_summary = authored["display_summary"]
    action.pii_receipts = receipts
    action.save(update_fields=["status", "responded_at", "action_payload", "display_summary", "pii_receipts"])
    record_action_audit(action, ActionAuditOutcome.APPROVED, responded_at=responded_at)

    target_at = fields.get("target_at")
    if isinstance(target_at, str):
        target_at = parse_datetime(target_at)
    try:
        command, _created = create_device_command(
            action.tenant,
            command_id=action.datebook_command_id,
            request_id=fields["request_id"],
            command_type=fields["command_type"],
            payload=fields["payload"],
            display_text=fields.get("display_text", ""),
            destination_name=fields.get("destination_name", ""),
            destination_fingerprint=fields.get("destination_fingerprint", ""),
            target_at=target_at,
        )
    except ProtocolError as exc:
        action.resolution_code = exc.code
        action.save(update_fields=["resolution_code"])
        record_action_audit(
            action,
            ActionAuditOutcome.FAILED,
            responded_at=responded_at,
            detail_code=exc.code,
        )
        _schedule_gate_changed(action)
        return _action_state(action)

    default_old = default_new = ""
    if set_default:
        default_old, default_new = _persist_destination_default(action, approved, command)

    action.resolution_code = ActionAuditOutcome.QUEUED
    action.save(update_fields=["resolution_code"])
    record_action_audit(
        action,
        ActionAuditOutcome.QUEUED,
        responded_at=responded_at,
        datebook_command_id=command.id,
        default_destination_old_fingerprint=default_old,
        default_destination_new_fingerprint=default_new,
    )

    from .notify import dispatch_device_command

    transaction.on_commit(lambda command=command: dispatch_device_command(command))
    _schedule_gate_changed(action)
    return _action_state(action)


def deny_datebook_action(action: PendingAction, *, responded_at) -> dict:
    action.status = ActionStatus.DENIED
    action.responded_at = responded_at
    action.resolution_code = ActionStatus.DENIED
    action.save(update_fields=["status", "responded_at", "resolution_code"])
    record_action_audit(action, ActionAuditOutcome.DENIED, responded_at=responded_at)
    _schedule_gate_changed(action)
    return _action_state(action)


def expire_datebook_action(action: PendingAction, *, reason: str | None = None) -> dict:
    if reason is None:
        reason = _datebook_expiry_reason(action)
    action.status = ActionStatus.EXPIRED
    action.resolution_code = reason
    action.responded_at = timezone.now()
    action.save(update_fields=["status", "resolution_code", "responded_at"])
    record_action_audit(
        action,
        ActionAuditOutcome.EXPIRED,
        responded_at=action.responded_at,
        detail_code=reason,
    )
    _schedule_gate_changed(action)
    return _action_state(action)


def request_datebook_action(
    tenant: Tenant,
    *,
    action_type: str,
    request_id: str,
    command_payload: dict,
    display_summary: str,
    direct_user_originated: bool,
    originating_channel: str | None = None,
    origin_stamp: OriginStamp | None = None,
) -> dict:
    """Create an idempotent gate bounded by 24 hours and the batch's earliest target."""

    if action_type not in DATEBOOK_ACTION_TYPES:
        raise ValueError("invalid datebook action type")

    try:
        with datebook_create_db_budget():
            existing = PendingAction.objects.filter(tenant=tenant, datebook_request_id=request_id).first()
        if existing is not None:
            return datebook_action_state(existing)
    except OperationalError as exc:
        raise _create_db_unavailable(tenant, exc) from exc

    reserved_command_id = uuid.uuid4()
    origin_stamp = origin_stamp or OriginStamp()
    app_review_available = originating_channel == "app" and not suppresses_real_transport(tenant)
    try:
        with datebook_create_db_budget():
            locked_tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
            existing = PendingAction.objects.filter(
                tenant=locked_tenant,
                datebook_request_id=request_id,
            ).first()
            if existing is not None:
                return datebook_action_state(existing)
            canonical_payload = deepcopy(command_payload)
            target_at = earliest_datebook_target_at(
                locked_tenant,
                canonical_payload.get("command_type"),
                canonical_payload.get("payload"),
            )
            canonical_payload["target_at"] = target_at.isoformat() if target_at else None
            duplicate = _recent_duplicate_action(
                locked_tenant,
                action_type=action_type,
                command_payload=canonical_payload,
            )
            if duplicate is not None:
                raise ProtocolError(
                    "duplicate_request",
                    409,
                    {
                        "existing_action_id": duplicate.id,
                        "message": "An identical approval is already pending. Do not create another.",
                    },
                )
            resolved_payload = _resolve_requested_destination(locked_tenant, action_type, canonical_payload)
            resolved_payload["direct_user_originated"] = direct_user_originated
            authored, receipts = _author_pending_datebook_fields(
                locked_tenant,
                action_payload=resolved_payload,
                display_summary=display_summary,
                seam="datebook.runtime.review.request",
                writer="runtime",
                defer_detection=True,
            )
            gate_now = timezone.now()
            action = PendingAction.objects.create(
                tenant=locked_tenant,
                action_type=action_type,
                action_payload=authored["action_payload"],
                display_summary=authored["display_summary"],
                pii_receipts=receipts,
                datebook_request_id=request_id,
                datebook_command_id=reserved_command_id,
                originating_channel=originating_channel or "",
                platform_channel="app" if app_review_available else "",
                delivery_state="available" if app_review_available else "",
                origin_kind=origin_stamp.kind,
                origin_cron_name=origin_stamp.cron_name,
                origin_run_id=origin_stamp.run_id,
                expires_at=_datebook_gate_expires_at(now=gate_now, target_at=target_at),
            )
            _schedule_gate_changed(action)
    except OperationalError as exc:
        raise _create_db_unavailable(tenant, exc) from exc
    except IntegrityError:
        try:
            with datebook_create_db_budget():
                action = PendingAction.objects.get(tenant=tenant, datebook_request_id=request_id)
            return datebook_action_state(action)
        except OperationalError as exc:
            raise _create_db_unavailable(tenant, exc) from exc

    # The app review sheet is a durable database surface, not a network
    # transport. Stamp it during the bounded insert above and avoid the generic
    # sender's second, formerly unbudgeted action.save() on the canary path.
    if app_review_available:
        return _action_state(action)

    from apps.actions.messaging import send_gate_confirmation

    try:
        with datebook_create_db_budget():
            delivered = send_gate_confirmation(
                tenant,
                action,
                originating_channel=originating_channel,
            )
    except OperationalError as exc:
        raise _create_db_unavailable(tenant, exc) from exc
    if delivered:
        return _action_state(action)

    try:
        with datebook_create_db_budget():
            action = PendingAction.objects.select_for_update().get(pk=action.pk)
            if action.status == ActionStatus.PENDING:
                return expire_datebook_action(action, reason=UNDELIVERABLE_REASON)
        return _action_state(action)
    except OperationalError as exc:
        raise _create_db_unavailable(tenant, exc) from exc
