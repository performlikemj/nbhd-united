"""Action gating API endpoints.

Container → Django endpoints for the confirmation flow:
- POST /api/v1/internal/runtime/<tenant_id>/gate/request — create a pending action
- GET  /api/v1/internal/runtime/<tenant_id>/gate/<action_id>/poll — poll for result
- POST /api/v1/gate/<action_id>/respond — callback from button press (internal)
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import (
    InternalAuthError,
    validate_internal_runtime_request,
)
from apps.router.document_write_guard import record_runtime_write_activity
from apps.tenants.models import Tenant

from .models import (
    ActionAuditLog,
    ActionStatus,
    ActionType,
    PendingAction,
)
from .services import record_action_audit, should_auto_approve

logger = logging.getLogger(__name__)


def _get_tenant_or_error(tenant_id: str):
    """Resolve tenant by UUID, return (tenant, None) or (None, Response)."""
    try:
        tenant = Tenant.objects.get(id=tenant_id)
        return tenant, None
    except Tenant.DoesNotExist:
        return None, Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)


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
        return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)


def _should_auto_approve(tenant: Tenant, action_type: str) -> bool:
    """Check if this action type should be auto-approved for this tenant."""
    return should_auto_approve(tenant, action_type)


def _is_starter_tier(tenant: Tenant) -> bool:
    """Check if tenant is on Starter tier (restricted from destructive actions)."""
    return getattr(tenant, "model_tier", "") == "starter"


# This message is relayed by the assistant into whatever channel the user is
# chatting on — including the iOS app, where App Review Guideline 3.1.1 forbids
# directing users to an external purchase ("steering"). The gate endpoint can't
# know the channel, so the copy must be store-safe everywhere: explain the
# restriction, no upgrade pitch, no billing URL.
STARTER_BLOCKED_MESSAGE = (
    "🔒 Destructive actions are not available on the Starter plan.\n\n"
    "Your agent tried to perform an irreversible action, but this is "
    "restricted on the Starter plan. Some AI models are more vulnerable "
    "to prompt injection — where unexpected input tricks the agent into "
    "doing something you didn't ask for.\n\n"
    "📖 Learn more about prompt injection:\n"
    "   https://genai.owasp.org/llmrisk/llm01-prompt-injection/"
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

        from apps.datebook.gate import is_datebook_action_type

        if is_datebook_action_type(action_type):
            return Response(
                {"error": "datebook_actions_use_request_create"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        record_runtime_write_activity(tenant)

        from apps.pii.store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {"action_payload": payload, "display_summary": display_summary},
            model_label="actions.PendingAction",
            seam="actions.runtime.gate_request",
            writer="runtime",
        )
        stored_payload = authored["action_payload"]
        stored_summary = authored["display_summary"]

        # Check if auto-approve is enabled for this action type
        if _should_auto_approve(tenant, action_type):
            # Log the auto-approval
            ActionAuditLog.objects.create(
                tenant=tenant,
                action_type=action_type,
                action_payload=stored_payload,
                display_summary=stored_summary,
                pii_receipts=receipts,
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
            action_payload=stored_payload,
            display_summary=stored_summary,
            pii_receipts=receipts,
        )

        # Send confirmation message via user's platform.
        # delivered=False means no Telegram/LINE channel exists (iOS-only user);
        # in that case we immediately mark the action EXPIRED and return a clear
        # "undeliverable" response so the container can surface a real error to
        # the user instead of polling for 5 minutes only to get "expired".
        from .messaging import send_gate_confirmation

        delivered = send_gate_confirmation(tenant, action)

        if not delivered:
            action.status = ActionStatus.EXPIRED
            action.save(update_fields=["status"])
            record_action_audit(action, ActionStatus.EXPIRED)
            logger.warning(
                "Gate request undeliverable (no channel): %s | %s",
                tenant.id,
                action_type,
            )
            return Response(
                {
                    "action_id": str(action.id),
                    "status": "undeliverable",
                    "reason": "no_channel",
                    "message": (
                        "This action requires confirmation via Telegram or LINE, "
                        "but no messaging channel is linked to your account. "
                        "Please connect Telegram or LINE to perform this action."
                    ),
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        logger.info(
            "Gate request created: %s | %s | %s",
            tenant.id,
            action_type,
            stored_summary[:60],
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
            action = PendingAction.objects.get(id=action_id, tenant_id=tenant_id)
        except PendingAction.DoesNotExist:
            return Response(
                {"error": "Action not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        datebook_state = None
        # Check for expiry — datebook gates use their locked typed transition;
        # generic gates retain the existing conditional update contract.
        if action.is_expired:
            from apps.datebook.gate import datebook_action_state, is_datebook_action_type

            if is_datebook_action_type(action.action_type):
                datebook_state = datebook_action_state(action)
                action.refresh_from_db()
                updated = datebook_state["state"] == "stale_review"
            else:
                updated = PendingAction.objects.filter(
                    id=action.id,
                    status=ActionStatus.PENDING,
                ).update(status=ActionStatus.EXPIRED)
                if updated:
                    action.status = ActionStatus.EXPIRED
                    record_action_audit(action, ActionStatus.EXPIRED)
                else:
                    # Another writer already resolved the row; re-read so the
                    # response below reflects the actual final status.
                    action.refresh_from_db(fields=["status"])

            if updated:
                # Clear stale platform buttons without allowing a messaging
                # failure to break the runtime's terminal response.
                try:
                    from .messaging import update_gate_message

                    update_gate_message(action)
                except Exception:
                    logger.warning("Failed to refresh gate message on expiry for action %s", action.id, exc_info=True)

        data = {"action_id": action.id, "status": action.status}
        if datebook_state is not None:
            data.update(datebook_state)
            data["status"] = action.status
        return Response(data, status=status.HTTP_200_OK)


class GateRespondView(APIView):
    """POST /api/v1/gate/<action_id>/respond

    Called internally by the button callback handler (Telegram/LINE).
    Uses deploy-secret auth since it's triggered by the Django poller itself.
    """

    permission_classes = [AllowAny]

    @classmethod
    def resolve_action(
        cls,
        *,
        action_id: int,
        response_action: str,
        tenant=None,
        destination_override=None,
        set_default: bool = False,
    ):
        """Resolve every channel through one locked gate/command transition seam."""

        if response_action not in ("approve", "deny"):
            return None, {"error": "action must be 'approve' or 'deny'"}, status.HTTP_400_BAD_REQUEST

        with transaction.atomic():
            actions = PendingAction.objects.select_for_update()
            if tenant is not None:
                actions = actions.filter(tenant=tenant)
            try:
                action = actions.get(id=action_id)
            except PendingAction.DoesNotExist:
                return None, {"error": "Action not found"}, status.HTTP_404_NOT_FOUND

            if action.status != ActionStatus.PENDING:
                return (
                    action,
                    {
                        "error": "action_already_resolved",
                        "status": action.status,
                    },
                    status.HTTP_409_CONFLICT,
                )

            from apps.datebook.gate import (
                STALE_REVIEW_REASON,
                approve_datebook_action,
                deny_datebook_action,
                expire_datebook_action,
                is_datebook_action_type,
            )

            if action.is_expired:
                if is_datebook_action_type(action.action_type):
                    data = expire_datebook_action(action, reason=STALE_REVIEW_REASON)
                else:
                    action.status = ActionStatus.EXPIRED
                    action.save(update_fields=["status"])
                    record_action_audit(action, ActionStatus.EXPIRED)
                    data = {"error": "Action expired", "status": "expired"}
                return action, data, status.HTTP_410_GONE

            now = timezone.now()
            if is_datebook_action_type(action.action_type):
                from apps.datebook.services import ProtocolError

                if not isinstance(set_default, bool):
                    return action, {"error": "invalid_set_default"}, status.HTTP_400_BAD_REQUEST
                if response_action == "deny" and (destination_override is not None or set_default):
                    return action, {"error": "destination_options_require_approval"}, status.HTTP_400_BAD_REQUEST
                if response_action == "approve":
                    try:
                        data = approve_datebook_action(
                            action,
                            responded_at=now,
                            destination_override=destination_override,
                            set_default=set_default,
                        )
                    except ProtocolError as exc:
                        return action, exc.as_data(), exc.status_code
                else:
                    data = deny_datebook_action(action, responded_at=now)
                data["status"] = action.status
                return action, data, status.HTTP_200_OK

            action.status = ActionStatus.APPROVED if response_action == "approve" else ActionStatus.DENIED
            action.responded_at = now
            action.save(update_fields=["status", "responded_at"])
            record_action_audit(action, action.status, responded_at=now)
            return action, {"status": action.status}, status.HTTP_200_OK

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

        action, data, response_status = self.resolve_action(
            action_id=action_id,
            response_action=request.data.get("action", ""),
        )
        if action is None:
            return Response(data, status=response_status)

        logger.info(
            "Gate response: %s | %s | %s | %s",
            action.tenant_id,
            action.action_type,
            data.get("status", action.status),
            action.display_summary[:60],
        )

        # Edit the confirmation message to show result (outside the atomic block
        # so a messaging hiccup cannot roll back the committed status change).
        from .messaging import update_gate_message

        if response_status in {status.HTTP_200_OK, status.HTTP_410_GONE}:
            update_gate_message(action)

        return Response(data, status=response_status)
