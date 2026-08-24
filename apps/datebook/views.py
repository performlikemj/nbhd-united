"""Consumer-JWT endpoints for the dormant Calendar & Reminders device lane."""

from __future__ import annotations

import json

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.actions.models import ActionStatus, PendingAction
from apps.actions.views import GateRespondView
from apps.pii.store_authoring import owner_store_representation
from apps.tenants.authentication import JWTAuthenticationWithRLS
from apps.tenants.models import Tenant

from .readiness import datebook_delivery_ready
from .services import (
    ProtocolError,
    claim_device_command,
    commit_sync_run,
    datebook_command_generation,
    finish_device_command,
    get_calendar_contexts,
    open_sync_run,
    register_gateway,
    replace_calendar_contexts,
    stage_sync_page,
    start_device_command,
)
from .throttles import DatebookCommandThrottle, DatebookReadThrottle, DatebookSyncPageThrottle

MAX_DATEBOOK_REQUEST_BYTES = 1_048_576
MAX_PENDING_GATE_ACTIONS = 20


class DatebookAPIView(APIView):
    """Base for consumers gated by enablement plus at least one consent."""

    authentication_classes = [JWTAuthenticationWithRLS]
    permission_classes = [IsAuthenticated]

    def authenticated_tenant(self, request):
        tenant = getattr(request.user, "tenant", None)
        if tenant is None:
            raise ProtocolError("no_tenant", status.HTTP_404_NOT_FOUND)
        tenant = Tenant.objects.filter(pk=tenant.pk).first()
        if tenant is None:
            raise ProtocolError("no_tenant", status.HTTP_404_NOT_FOUND)
        if tenant.status == Tenant.Status.SUSPENDED:
            raise ProtocolError("suspended", status.HTTP_403_FORBIDDEN)
        return tenant

    def tenant(self, request):
        tenant = self.authenticated_tenant(request)
        if not tenant.datebook_enabled:
            raise ProtocolError("datebook_disabled", status.HTTP_409_CONFLICT)
        if not (tenant.datebook_events_consent_at or tenant.datebook_reminders_consent_at):
            raise ProtocolError("consent_required", status.HTTP_409_CONFLICT)
        return tenant

    def handle_exception(self, exc):
        if isinstance(exc, ProtocolError):
            return Response(exc.as_data(), status=exc.status_code)
        return super().handle_exception(exc)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def body(request) -> dict:
        content_length = request.META.get("CONTENT_LENGTH")
        try:
            if content_length and int(content_length) > MAX_DATEBOOK_REQUEST_BYTES:
                raise ProtocolError("request_too_large")
        except (TypeError, ValueError):
            raise ProtocolError("invalid_content_length") from None
        data = request.data
        if not isinstance(data, dict):
            raise ProtocolError("invalid_body")
        try:
            encoded_size = len(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_body") from exc
        if encoded_size > MAX_DATEBOOK_REQUEST_BYTES:
            raise ProtocolError("request_too_large")
        return data


class GatewayRegisterView(DatebookAPIView):
    """Bootstrap gateway registration using enablement, never consent/manifest."""

    throttle_classes = [DatebookReadThrottle]

    def post(self, request):
        tenant = self.authenticated_tenant(request)
        if not tenant.datebook_enabled:
            raise ProtocolError("datebook_disabled", status.HTTP_409_CONFLICT)
        data = self.body(request)
        gateway, taken_over, tenant = register_gateway(
            tenant,
            installation_id=data.get("installation_id"),
            takeover=data.get("takeover", False),
            events_consent=data.get("events_consent"),
            reminders_consent=data.get("reminders_consent"),
        )
        return Response(
            {
                "installation_id": gateway.installation_id,
                "gateway_epoch": gateway.gateway_epoch,
                "generation": gateway.current_generation,
                "gateway_status": gateway.status,
                "taken_over": taken_over,
                "scopes": {
                    "events": {
                        "consent_at": (
                            tenant.datebook_events_consent_at.isoformat() if tenant.datebook_events_consent_at else None
                        ),
                        "full_snapshot_required": gateway.events_full_snapshot_required,
                    },
                    "reminders": {
                        "consent_at": (
                            tenant.datebook_reminders_consent_at.isoformat()
                            if tenant.datebook_reminders_consent_at
                            else None
                        ),
                        "full_snapshot_required": gateway.reminders_full_snapshot_required,
                    },
                },
                "delivery_ready": datebook_delivery_ready(tenant),
            }
        )


def _calendar_context_data(row, tenant) -> dict:
    return owner_store_representation(
        row,
        tenant,
        {
            "calendar_fingerprint": row.calendar_fingerprint,
            "entity_scope": row.entity_scope,
            "included": row.included,
            "container_title": row.container_title,
            "source_title": row.source_title,
            "source_type": row.source_type,
            "context_note": row.context_note,
            "updated_at": row.updated_at.isoformat(),
        },
        model_label="datebook.CalendarContext",
    )


def _calendar_context_response(rows, tenant) -> dict:
    calendars = [_calendar_context_data(row, tenant) for row in rows]
    return {"calendar_count": len(calendars), "calendars": calendars}


class CalendarContextsView(DatebookAPIView):
    """Replace or restore the active installation's non-default calendar prefs."""

    throttle_classes = [DatebookReadThrottle]

    def get(self, request):
        tenant = self.tenant(request)
        raw_epoch = request.query_params.get("gateway_epoch")
        try:
            gateway_epoch = int(raw_epoch)
        except (TypeError, ValueError):
            raise ProtocolError("invalid_gateway_epoch") from None
        rows = get_calendar_contexts(
            tenant,
            installation_id=request.query_params.get("installation_id"),
            gateway_epoch=gateway_epoch,
        )
        return Response(_calendar_context_response(rows, tenant))

    def put(self, request):
        tenant = self.tenant(request)
        data = self.body(request)
        rows = replace_calendar_contexts(
            tenant,
            installation_id=data.get("installation_id"),
            gateway_epoch=data.get("gateway_epoch"),
            calendars=data.get("calendars"),
        )
        return Response(_calendar_context_response(rows, tenant))


def _pending_gate_action_data(action: PendingAction, tenant: Tenant) -> dict:
    represented = owner_store_representation(
        action,
        tenant,
        {
            "action_payload": action.action_payload,
            "display_summary": action.display_summary,
        },
        model_label="actions.PendingAction",
    )
    return {
        "action_id": action.id,
        "action_type": action.action_type,
        "payload": represented["action_payload"],
        "display_summary": represented["display_summary"],
        "originating_channel": action.originating_channel,
        "created_at": action.created_at.isoformat(),
        "expires_at": action.expires_at.isoformat(),
    }


class PendingGateActionsView(DatebookAPIView):
    """List the owner's oldest pending datebook reviews; clients must load fast."""

    throttle_classes = [DatebookReadThrottle]

    def get(self, request):
        tenant = self.tenant(request)
        from .gate import DATEBOOK_ACTION_TYPES, DATEBOOK_GATE_REVIEW_WINDOW_SECONDS

        actions = PendingAction.objects.filter(
            tenant=tenant,
            status=ActionStatus.PENDING,
            expires_at__gt=timezone.now(),
            action_type__in=DATEBOOK_ACTION_TYPES,
        ).order_by("created_at", "id")[:MAX_PENDING_GATE_ACTIONS]
        return Response(
            {
                "actions": [_pending_gate_action_data(action, tenant) for action in actions],
                "review_window_seconds": DATEBOOK_GATE_REVIEW_WINDOW_SECONDS,
            }
        )


class RespondGateActionView(DatebookAPIView):
    """Resolve an owned datebook review through the shared locked seam."""

    throttle_classes = [DatebookCommandThrottle]

    def post(self, request, action_id: int):
        tenant = self.tenant(request)
        data = self.body(request)
        from .gate import DATEBOOK_ACTION_TYPES

        if not PendingAction.objects.filter(
            id=action_id,
            tenant=tenant,
            action_type__in=DATEBOOK_ACTION_TYPES,
        ).exists():
            return Response({"error": "Action not found"}, status=status.HTTP_404_NOT_FOUND)
        action, response_data, response_status = GateRespondView.resolve_action(
            action_id=action_id,
            response_action=data.get("response", ""),
            tenant=tenant,
            destination_override=data.get("destination_override"),
            set_default=data.get("set_default", False),
        )
        if action is not None and response_status in {status.HTTP_200_OK, status.HTTP_410_GONE}:
            from apps.actions.messaging import update_gate_message

            update_gate_message(action)
        return Response(response_data, status=response_status)


def _run_data(run, *, idempotent: bool) -> dict:
    return {
        "run_id": str(run.id),
        "client_run_id": run.client_run_id,
        "idempotent": idempotent,
        "server_now": run.server_now.isoformat(),
        "event_window": {
            "start": run.event_window_start.isoformat(),
            "end": run.event_window_end.isoformat(),
        },
        "base_generation": run.base_generation,
        "gateway_epoch": run.gateway_epoch,
        "scopes": {
            "events": {
                "enabled": run.events_in_scope,
                "authorization": run.events_authorization,
                "coverage_complete": run.events_coverage_complete,
                "committable": run.events_committable,
                "full_snapshot_required": run.events_full_snapshot,
            },
            "reminders": {
                "enabled": run.reminders_in_scope,
                "authorization": run.reminders_authorization,
                "coverage_complete": run.reminders_coverage_complete,
                "committable": run.reminders_committable,
                "full_snapshot_required": run.reminders_full_snapshot,
            },
        },
    }


class SyncOpenView(DatebookAPIView):
    throttle_classes = [DatebookReadThrottle]

    def post(self, request):
        tenant = self.tenant(request)
        data = self.body(request)
        run, created = open_sync_run(
            tenant,
            installation_id=data.get("installation_id"),
            gateway_epoch=data.get("gateway_epoch"),
            client_run_id=data.get("client_run_id"),
            events=data.get("events"),
            reminders=data.get("reminders"),
        )
        return Response(_run_data(run, idempotent=not created))


class SyncPageView(DatebookAPIView):
    throttle_classes = [DatebookSyncPageThrottle]

    def post(self, request):
        tenant = self.tenant(request)
        data = self.body(request)
        outcome = stage_sync_page(
            tenant,
            run_id=data.get("run_id"),
            page_index=data.get("page_index"),
            installation_id=data.get("installation_id"),
            gateway_epoch=data.get("gateway_epoch"),
            events=data.get("events", []),
            reminders=data.get("reminders", []),
        )
        return Response(outcome)


class SyncCommitView(DatebookAPIView):
    throttle_classes = [DatebookReadThrottle]

    def post(self, request):
        tenant = self.tenant(request)
        data = self.body(request)
        outcome = commit_sync_run(
            tenant,
            run_id=data.get("run_id"),
            installation_id=data.get("installation_id"),
            gateway_epoch=data.get("gateway_epoch"),
            events=data.get("events"),
            reminders=data.get("reminders"),
        )
        return Response(outcome)


def _command_data(command, tenant) -> dict:
    represented = owner_store_representation(
        command,
        tenant,
        {
            "id": str(command.id),
            "request_id": command.request_id,
            "command_type": command.command_type,
            "state": command.state,
            "item_count": command.item_count,
            "target_installation_id": command.target_installation_id,
            "target_gateway_epoch": command.target_gateway_epoch,
            "destination_fingerprint": command.destination_fingerprint,
            "destination_name": command.destination_name,
            "display_text": command.display_text,
            "payload": command.payload,
            "lease_token": str(command.lease_token) if command.lease_token else None,
            "lease_expires_at": command.lease_expires_at.isoformat() if command.lease_expires_at else None,
            "expires_at": command.expires_at.isoformat(),
            "target_at": command.target_at.isoformat() if command.target_at else None,
            "execution_status": command.execution_status,
            "mirror_status": command.mirror_status,
            "safe_error": command.safe_error,
            "result_display": command.result_display,
            "pii_receipts": command.pii_receipts,
        },
        model_label="datebook.DeviceCommand",
    )
    represented["datebook_command_generation"] = datebook_command_generation(tenant)
    return represented


class CommandClaimView(DatebookAPIView):
    throttle_classes = [DatebookCommandThrottle]

    def post(self, request):
        tenant = self.tenant(request)
        data = self.body(request)
        command = claim_device_command(
            tenant,
            installation_id=data.get("installation_id"),
            gateway_epoch=data.get("gateway_epoch"),
        )
        return Response(
            {
                "command": _command_data(command, tenant) if command else None,
                "datebook_command_generation": datebook_command_generation(tenant),
            }
        )


class CommandStartView(DatebookAPIView):
    throttle_classes = [DatebookCommandThrottle]

    def post(self, request, command_id):
        tenant = self.tenant(request)
        data = self.body(request)
        command, idempotent = start_device_command(
            tenant,
            command_id=command_id,
            lease_token=data.get("lease_token"),
            installation_id=data.get("installation_id"),
            gateway_epoch=data.get("gateway_epoch"),
            destination_fingerprint=data.get("destination_fingerprint", ""),
        )
        return Response(
            {
                "command_id": str(command.id),
                "state": command.state,
                "execution_status": command.execution_status,
                "datebook_command_generation": datebook_command_generation(tenant),
                "idempotent": idempotent,
            }
        )


class CommandResultView(DatebookAPIView):
    throttle_classes = [DatebookCommandThrottle]

    def post(self, request, command_id):
        tenant = self.tenant(request)
        data = self.body(request)
        journaled_at = parse_datetime(data.get("journaled_at")) if isinstance(data.get("journaled_at"), str) else None
        command, idempotent = finish_device_command(
            tenant,
            command_id=command_id,
            lease_token=data.get("lease_token"),
            result_id=data.get("result_id"),
            execution_status=data.get("execution_status"),
            mirror_status=data.get("mirror_status"),
            safe_error=data.get("safe_error", ""),
            result_identifiers=data.get("result_identifiers", {}),
            result_display=data.get("result_display", ""),
            journaled_at=journaled_at,
        )
        return Response(
            {
                "command_id": str(command.id),
                "state": command.state,
                "execution_status": command.execution_status,
                "mirror_status": command.mirror_status,
                "safe_error": command.safe_error,
                "datebook_command_generation": datebook_command_generation(tenant),
                "idempotent": idempotent,
            }
        )
