"""Shared action-gate decisions and immutable audit transition helpers."""

from __future__ import annotations

from django.utils import timezone

from apps.tenants.models import Tenant

from .models import ActionAuditLog, GatePreference, PendingAction


def should_auto_approve(tenant: Tenant, action_type: str) -> bool:
    """Apply the existing global and per-type gate preference machinery."""

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
) -> ActionAuditLog:
    """Append one audit row without rewriting any earlier transition."""

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
