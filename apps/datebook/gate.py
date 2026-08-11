"""Datebook-specific orchestration on top of the shared action review gate."""

from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.actions.models import ActionAuditOutcome, ActionStatus, ActionType, PendingAction
from apps.actions.services import record_action_audit, should_auto_approve
from apps.pii.store_authoring import author_store_fields, owner_store_representation
from apps.tenants.models import Tenant

DATEBOOK_ACTION_TYPES = frozenset({ActionType.CALENDAR_CREATE, ActionType.REMINDER_CREATE})
UNDELIVERABLE_REASON = "needs_app_update_or_linked_chat"
UNDELIVERABLE_MESSAGE = "This calendar request needs the app update or a linked chat channel to approve."


def is_datebook_action_type(action_type: str) -> bool:
    return action_type in DATEBOOK_ACTION_TYPES


def _action_state(action: PendingAction) -> dict:
    base = {
        "action_id": action.id,
        "command_id": str(action.datebook_command_id),
    }
    if action.status == ActionStatus.PENDING:
        return {**base, "state": "approval_pending", "expires_at": action.expires_at.isoformat()}
    if action.status == ActionStatus.DENIED:
        return {**base, "state": "denied"}
    if action.status == ActionStatus.EXPIRED:
        if action.resolution_code == UNDELIVERABLE_REASON:
            return {
                **base,
                "state": "undeliverable",
                "reason": UNDELIVERABLE_REASON,
                "message": UNDELIVERABLE_MESSAGE,
            }
        return {**base, "state": "expired"}
    if action.resolution_code and action.resolution_code != ActionAuditOutcome.QUEUED:
        return {**base, "state": action.resolution_code}
    return {**base, "state": "approved_queued"}


def datebook_action_state(action: PendingAction) -> dict:
    action.refresh_from_db()
    return _action_state(action)


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
    payload = dict(represented_payload)
    target_at = payload.get("target_at")
    if isinstance(target_at, str):
        payload["target_at"] = parse_datetime(target_at)
    return payload


def approve_datebook_action(action: PendingAction, *, responded_at) -> dict:
    """Persist approval, command creation, links, and audit rows atomically."""

    from .services import ProtocolError, create_device_command

    action.status = ActionStatus.APPROVED
    action.responded_at = responded_at
    action.save(update_fields=["status", "responded_at"])
    record_action_audit(action, ActionAuditOutcome.APPROVED, responded_at=responded_at)

    fields = _rehydrated_command_fields(action)
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
            target_at=fields.get("target_at"),
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
        return _action_state(action)

    action.resolution_code = ActionAuditOutcome.QUEUED
    action.save(update_fields=["resolution_code"])
    record_action_audit(
        action,
        ActionAuditOutcome.QUEUED,
        responded_at=responded_at,
        datebook_command_id=command.id,
    )

    from .notify import notify_device_command

    transaction.on_commit(lambda command=command: notify_device_command(command))
    return _action_state(action)


def deny_datebook_action(action: PendingAction, *, responded_at) -> dict:
    action.status = ActionStatus.DENIED
    action.responded_at = responded_at
    action.resolution_code = ActionStatus.DENIED
    action.save(update_fields=["status", "responded_at", "resolution_code"])
    record_action_audit(action, ActionAuditOutcome.DENIED, responded_at=responded_at)
    return _action_state(action)


def expire_datebook_action(action: PendingAction, *, reason: str = "") -> dict:
    action.status = ActionStatus.EXPIRED
    action.resolution_code = reason
    action.save(update_fields=["status", "resolution_code"])
    record_action_audit(
        action,
        ActionAuditOutcome.EXPIRED,
        responded_at=timezone.now(),
        detail_code=reason,
    )
    return _action_state(action)


def request_datebook_action(
    tenant: Tenant,
    *,
    action_type: str,
    request_id: str,
    command_payload: dict,
    display_summary: str,
    direct_user_originated: bool,
) -> dict:
    """Create an idempotent gate request, respecting direct-turn auto-approve."""

    if action_type not in DATEBOOK_ACTION_TYPES:
        raise ValueError("invalid datebook action type")

    existing = PendingAction.objects.filter(tenant=tenant, datebook_request_id=request_id).first()
    if existing is not None:
        return _action_state(existing)

    reserved_command_id = uuid.uuid4()
    try:
        with transaction.atomic():
            locked_tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
            existing = PendingAction.objects.filter(
                tenant=locked_tenant,
                datebook_request_id=request_id,
            ).first()
            if existing is not None:
                return _action_state(existing)
            authored, receipts = author_store_fields(
                locked_tenant,
                {
                    "action_payload": command_payload,
                    "display_summary": display_summary,
                },
                model_label="actions.PendingAction",
                seam="datebook.runtime.review.request",
                writer="runtime",
            )
            action = PendingAction.objects.create(
                tenant=locked_tenant,
                action_type=action_type,
                action_payload=authored["action_payload"],
                display_summary=authored["display_summary"],
                pii_receipts=receipts,
                datebook_request_id=request_id,
                datebook_command_id=reserved_command_id,
            )
            if direct_user_originated and should_auto_approve(locked_tenant, action_type):
                return approve_datebook_action(action, responded_at=timezone.now())
    except IntegrityError:
        action = PendingAction.objects.get(tenant=tenant, datebook_request_id=request_id)
        return _action_state(action)

    from apps.actions.messaging import send_gate_confirmation

    if send_gate_confirmation(tenant, action):
        return _action_state(action)

    with transaction.atomic():
        action = PendingAction.objects.select_for_update().get(pk=action.pk)
        if action.status == ActionStatus.PENDING:
            return expire_datebook_action(action, reason=UNDELIVERABLE_REASON)
    return _action_state(action)
