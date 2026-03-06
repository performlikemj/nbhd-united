"""Action gating API endpoints.

Container → Django endpoints for the confirmation flow:
- POST /api/v1/internal/runtime/<tenant_id>/gate/request — create a pending action
- GET  /api/v1/internal/runtime/<tenant_id>/gate/<action_id>/poll — poll for result
- POST /api/v1/gate/<action_id>/respond — callback from button press (internal)
"""
from __future__ import annotations

import logging
from uuid import UUID

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import (
    InternalAuthError,
    validate_internal_runtime_request,
)
from apps.tenants.models import Tenant

from .models import (
    ActionAuditLog,
    ActionStatus,
    ActionType,
    GatePreference,
    PendingAction,
)

logger = logging.getLogger(__name__)


def _get_tenant_or_error(tenant_id: str):
    """Resolve tenant by UUID, return (tenant, None) or (None, Response)."""
    try:
        tenant = Tenant.objects.get(id=tenant_id)
        return tenant, None
    except Tenant.DoesNotExist:
        return None, Response(
            {"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND
        )


def _validate_internal_auth(request, tenant_id: str):
    """Validate internal runtime auth headers. Returns error Response or None."""
    try:
        validate_internal_runtime_request(
            provided_key=request.headers.get("X-Internal-Key", ""),
            provided_tenant_id=request.headers.get("X-Tenant-Id", str(tenant_id)),
            expected_tenant_id=str(tenant_id),
        )
        return None
    except InternalAuthError as e:
        return Response(
            {"error": str(e)}, status=status.HTTP_403_FORBIDDEN
        )


def _should_auto_approve(tenant: Tenant, action_type: str) -> bool:
    """Check if this action type should be auto-approved for this tenant."""
    # Master switch off = auto-approve everything (user acknowledged risk)
    if not tenant.gate_all_actions and tenant.gate_acknowledged_risk:
        return True

    # Check per-action-type preference
    try:
        pref = GatePreference.objects.get(
            tenant=tenant, action_type=action_type
        )
        return not pref.require_confirmation
    except GatePreference.DoesNotExist:
        # Default: require confirmation
        return False


def _is_starter_tier(tenant: Tenant) -> bool:
    """Check if tenant is on Starter tier (restricted from destructive actions)."""
    return getattr(tenant, "model_tier", "") == "starter"


STARTER_BLOCKED_MESSAGE = (
    "🔒 Destructive actions are not available on the Starter plan.\n\n"
    "Your agent tried to perform an irreversible action, but this is "
    "restricted on the Starter plan. Some AI models are more vulnerable "
    "to prompt injection — where unexpected input tricks the agent into "
    "doing something you didn't ask for.\n\n"
    "Upgrading to Premium gives you:\n"
    "• A more resilient AI model (Claude by Anthropic)\n"
    "• Full Google Workspace access with confirmation prompts\n"
    "• Every irreversible action requires your explicit approval\n\n"
    "📖 Learn more about prompt injection:\n"
    "   https://genai.owasp.org/llmrisk/llm01-prompt-injection/\n\n"
    "⬆️ Upgrade: https://neighborhoodunited.org/billing"
)


class GateRequestView(APIView):
    """POST /api/v1/internal/runtime/<tenant_id>/gate/request

    Called by the agent container to request approval for a destructive action.
    """
    permission_classes = [AllowAny]

    def post(self, request, tenant_id: UUID):
        auth_error = _validate_internal_auth(request, str(tenant_id))
        if auth_error:
            return auth_error

        tenant, err = _get_tenant_or_error(str(tenant_id))
        if err:
            return err

        action_type = request.data.get("action_type", "")
        payload = request.data.get("payload", {})
        display_summary = request.data.get("display_summary", "")

        # Validate action_type
        valid_types = [choice[0] for choice in ActionType.choices]
        if action_type not in valid_types:
            return Response(
                {"error": f"Invalid action_type. Must be one of: {valid_types}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not display_summary:
            return Response(
                {"error": "display_summary is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Starter tier: block entirely
        if _is_starter_tier(tenant):
            return Response(
                {
                    "status": "blocked",
                    "tier": "starter",
                    "message": STARTER_BLOCKED_MESSAGE,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Check if auto-approve is enabled for this action type
        if _should_auto_approve(tenant, action_type):
            # Log the auto-approval
            ActionAuditLog.objects.create(
                tenant=tenant,
                action_type=action_type,
                action_payload=payload,
                display_summary=display_summary,
                result=ActionStatus.APPROVED,
                responded_at=timezone.now(),
            )
            return Response(
                {
                    "action_id": None,
                    "status": "approved",
                    "auto_approved": True,
                },
                status=status.HTTP_200_OK,
            )

        # Create pending action
        action = PendingAction.objects.create(
            tenant=tenant,
            action_type=action_type,
            action_payload=payload,
            display_summary=display_summary,
        )

        # TODO: Send confirmation message via Telegram/LINE (Block 3)
        # send_gate_confirmation(tenant, action)

        logger.info(
            "Gate request created: %s | %s | %s",
            tenant.id, action_type, display_summary[:60],
        )

        return Response(
            {
                "action_id": str(action.id),
                "status": "pending",
                "expires_at": action.expires_at.isoformat(),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class GatePollView(APIView):
    """GET /api/v1/internal/runtime/<tenant_id>/gate/<action_id>/poll

    Called by the agent container to check if the user has responded.
    """
    permission_classes = [AllowAny]

    def get(self, request, tenant_id: UUID, action_id: int):
        auth_error = _validate_internal_auth(request, str(tenant_id))
        if auth_error:
            return auth_error

        try:
            action = PendingAction.objects.get(
                id=action_id, tenant_id=tenant_id
            )
        except PendingAction.DoesNotExist:
            return Response(
                {"error": "Action not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check for expiry
        if action.is_expired:
            action.status = ActionStatus.EXPIRED
            action.save(update_fields=["status"])
            # Log it
            ActionAuditLog.objects.create(
                tenant_id=tenant_id,
                action_type=action.action_type,
                action_payload=action.action_payload,
                display_summary=action.display_summary,
                result=ActionStatus.EXPIRED,
            )

        return Response(
            {
                "action_id": action.id,
                "status": action.status,
            },
            status=status.HTTP_200_OK,
        )


class GateRespondView(APIView):
    """POST /api/v1/gate/<action_id>/respond

    Called internally by the button callback handler (Telegram/LINE).
    Uses deploy-secret auth since it's triggered by the Django poller itself.
    """
    permission_classes = [AllowAny]

    def post(self, request, action_id: int):
        from django.conf import settings as django_settings

        # Auth: deploy-secret (this is called by Django's own poller)
        deploy_secret = getattr(django_settings, "DEPLOY_SECRET", None)
        if not deploy_secret:
            return Response(
                {"error": "Server not configured for gate responses"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        provided = request.headers.get("X-Deploy-Secret", "")
        if provided != deploy_secret:
            return Response(
                {"error": "Unauthorized"},
                status=status.HTTP_403_FORBIDDEN,
            )

        response_action = request.data.get("action", "")
        if response_action not in ("approve", "deny"):
            return Response(
                {"error": "action must be 'approve' or 'deny'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            action = PendingAction.objects.get(id=action_id)
        except PendingAction.DoesNotExist:
            return Response(
                {"error": "Action not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if action.status != ActionStatus.PENDING:
            return Response(
                {
                    "error": f"Action already resolved: {action.status}",
                    "status": action.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Check expiry
        if action.is_expired:
            action.status = ActionStatus.EXPIRED
            action.save(update_fields=["status"])
            ActionAuditLog.objects.create(
                tenant=action.tenant,
                action_type=action.action_type,
                action_payload=action.action_payload,
                display_summary=action.display_summary,
                result=ActionStatus.EXPIRED,
            )
            return Response(
                {"error": "Action expired", "status": "expired"},
                status=status.HTTP_410_GONE,
            )

        # Apply response
        now = timezone.now()
        if response_action == "approve":
            action.status = ActionStatus.APPROVED
        else:
            action.status = ActionStatus.DENIED
        action.responded_at = now
        action.save(update_fields=["status", "responded_at"])

        # Audit log
        ActionAuditLog.objects.create(
            tenant=action.tenant,
            action_type=action.action_type,
            action_payload=action.action_payload,
            display_summary=action.display_summary,
            result=action.status,
            responded_at=now,
        )

        logger.info(
            "Gate response: %s | %s | %s | %s",
            action.tenant_id, action.action_type,
            action.status, action.display_summary[:60],
        )

        # TODO: Edit the Telegram/LINE message to show result (Block 3)
        # update_gate_message(action)

        return Response(
            {"status": action.status},
            status=status.HTTP_200_OK,
        )
