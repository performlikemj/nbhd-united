"""Shared action-gate decisions and immutable audit transition helpers."""

from __future__ import annotations

from django.utils import timezone

from apps.tenants.models import Tenant

from .models import ActionAuditLog, GatePreference, PendingAction

DATEBOOK_REVIEW_ALWAYS_ACTION_TYPES = frozenset({"calendar_create", "reminder_create", "cron_create"})


def should_auto_approve(tenant: Tenant, action_type: str) -> bool:
    """Apply the existing global and per-type gate preference machinery."""

    if action_type in DATEBOOK_REVIEW_ALWAYS_ACTION_TYPES:
        return False
    if not tenant.gate_all_actions and tenant.gate_acknowledged_risk:
        return True
    try:
        preference = GatePreference.objects.get(tenant=tenant, action_type=action_type)
    except GatePreference.DoesNotExist:
        return False
    return not preference.require_confirmation


def record_action_audit(
    action: PendingAction,
    result: str,
    *,
    responded_at=None,
    datebook_command_id=None,
    detail_code: str = "",
    requested_destination_fingerprint: str = "",
    approved_destination_fingerprint: str = "",
    default_destination_old_fingerprint: str = "",
    default_destination_new_fingerprint: str = "",
) -> ActionAuditLog:
    """Append one audit row without rewriting any earlier transition."""

    payload = action.action_payload if isinstance(action.action_payload, dict) else {}
    requested = payload.get("requested_destination")
    approved = payload.get("approved_destination")
    if not requested_destination_fingerprint and isinstance(requested, dict):
        requested_destination_fingerprint = str(requested.get("fingerprint") or "")
    if not approved_destination_fingerprint and isinstance(approved, dict):
        approved_destination_fingerprint = str(approved.get("fingerprint") or "")

    return ActionAuditLog.objects.create(
        tenant=action.tenant,
        action_type=action.action_type,
        action_payload=action.action_payload,
        display_summary=action.display_summary,
        pii_receipts=action.pii_receipts,
        result=result,
        responded_at=responded_at,
        datebook_command_id=datebook_command_id or action.datebook_command_id,
        detail_code=detail_code,
        requested_destination_fingerprint=requested_destination_fingerprint,
        approved_destination_fingerprint=approved_destination_fingerprint,
        default_destination_old_fingerprint=default_destination_old_fingerprint,
        default_destination_new_fingerprint=default_destination_new_fingerprint,
    )


def record_datebook_command_transition(command, result: str, *, detail_code: str = "") -> None:
    """Append a lifecycle audit when a command originated from the review gate."""

    action = PendingAction.objects.select_related("tenant").filter(datebook_command_id=command.id).first()
    if action is None:
        return
    record_action_audit(
        action,
        result,
        responded_at=timezone.now(),
        datebook_command_id=command.id,
        detail_code=detail_code,
    )
