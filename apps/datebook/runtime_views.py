"""Internal runtime views for the dormant Calendar & Reminders plugin."""

from __future__ import annotations

from datetime import datetime, time
from uuid import UUID

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.actions.models import ActionType, PendingAction
from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.pii.egress import KnownValueResponseGuardMixin
from apps.router.document_write_guard import record_runtime_write_activity
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

from .agenda import agenda_items, agenda_window
from .gate import datebook_action_state, request_datebook_action
from .models import DatebookGateway, DeviceCommand
from .readiness import datebook_delivery_ready
from .services import (
    ProtocolError,
    _validate_command_payload,
    datebook_command_generation,
    sweep_device_commands,
)
from .throttles import DatebookRuntimeCreateThrottle

AGENDA_ITEM_LIMIT = 200
RUNTIME_CREATE_BYTES = 64 * 1024


class _DatebookResponseGuard(KnownValueResponseGuardMixin):
    """Known-value coverage for every model-facing datebook text field."""

    pii_egress_seam = "datebook_runtime_response"
    pii_egress_text_fields = frozenset(
        {
            "title",
            "location",
            "notes",
            "calendar_title",
            "list_title",
            "source_title",
            "display_text",
        }
    )


class _DatebookRuntimeView(_DatebookResponseGuard, APIView):
    permission_classes = [AllowAny]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


def _internal_auth_or_401(request, tenant_id: UUID) -> Response | None:
    try:
        validate_internal_runtime_request(
            provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
            provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
            expected_tenant_id=str(tenant_id),
        )
    except InternalAuthError as exc:
        return Response(
            {"error": "internal_auth_failed", "detail": str(exc)},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    set_rls_context(tenant_id=tenant_id, service_role=True)
    return None


def _tenant_or_404(tenant_id: UUID) -> Tenant | Response:
    try:
        return Tenant.objects.get(pk=tenant_id)
    except Tenant.DoesNotExist:
        return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)


def _bounded_int(raw_value, *, default: int, minimum: int, maximum: int, name: str) -> int:
    if raw_value in (None, ""):
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}_invalid") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name}_out_of_range")
    return value


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _scope_status(tenant: Tenant, gateway: DatebookGateway | None, scope: str) -> dict:
    consented = bool(getattr(tenant, f"datebook_{scope}_consent_at"))
    return {
        "consented": consented,
        "authorization": getattr(gateway, f"{scope}_authorization", "unavailable") if gateway else "unavailable",
        "last_complete_sync_at": _iso(getattr(gateway, f"{scope}_last_complete_sync_at", None)) if gateway else None,
        "gateway_status": gateway.status if gateway else "unavailable",
    }


def _first_target_at(tenant: Tenant, command_type: str, payload: dict):
    """Return the earliest absolute execution cutoff represented by a batch."""

    from django.utils.dateparse import parse_date, parse_datetime

    from apps.common.tenant_tz import tenant_tz

    candidates = []
    key = "time" if command_type == DeviceCommand.CommandType.CALENDAR_CREATE else "due"
    for item in payload["items"]:
        tagged = item.get(key, {"kind": "none"})
        kind = tagged.get("kind")
        if kind == "all_day":
            raw = tagged.get("start_date") or tagged.get("date")
            parsed = parse_date(raw) if isinstance(raw, str) else None
            if parsed is not None:
                candidates.append(datetime.combine(parsed, time.max, tzinfo=tenant_tz(tenant)))
        elif kind == "zoned":
            raw = tagged.get("start_at") or tagged.get("due_at")
            parsed = parse_datetime(raw) if isinstance(raw, str) else None
            if parsed is not None:
                candidates.append(parsed)
        elif kind == "floating":
            raw = tagged.get("start_local") or tagged.get("due_local")
            parsed = parse_datetime(raw) if isinstance(raw, str) else None
            if parsed is not None:
                candidates.append(parsed.replace(tzinfo=tenant_tz(tenant)))
    return min(candidates) if candidates else None


def _display_summary(command_type: str, payload: dict) -> str:
    labels = [item["title"] for item in payload["items"]]
    noun = "calendar event" if command_type == DeviceCommand.CommandType.CALENDAR_CREATE else "Apple Reminder"
    if len(labels) == 1:
        return f"Create {noun}: {labels[0]}"[:500]
    return f"Create {len(labels)} {noun}s: {', '.join(labels)}"[:500]


class RuntimeAgendaView(_DatebookRuntimeView):
    """Return a bounded active-mirror agenda with absolute freshness metadata."""

    def get(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        if not datebook_delivery_ready(tenant):
            return Response({"state": "datebook_disabled"}, status=status.HTTP_409_CONFLICT)

        try:
            days_ahead = _bounded_int(
                request.query_params.get("days_ahead"),
                default=7,
                minimum=0,
                maximum=60,
                name="days_ahead",
            )
            days_back = _bounded_int(
                request.query_params.get("days_back"),
                default=0,
                minimum=0,
                maximum=30,
                name="days_back",
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        entity = request.query_params.get("entity", "both")
        if entity not in {"events", "reminders", "both"}:
            return Response({"error": "entity_invalid"}, status=status.HTTP_400_BAD_REQUEST)

        gateway = DatebookGateway.objects.filter(tenant=tenant, status=DatebookGateway.Status.ACTIVE).first()
        items, truncated = agenda_items(
            tenant,
            days_back=days_back,
            days_ahead=days_ahead,
            entity=entity,
            limit=AGENDA_ITEM_LIMIT,
        )
        start_day, end_day, start_at, end_at = agenda_window(
            tenant,
            days_back=days_back,
            days_ahead=days_ahead,
        )
        scopes = {}
        if entity in {"events", "both"}:
            scopes["events"] = _scope_status(tenant, gateway, "events")
        if entity in {"reminders", "both"}:
            scopes["reminders"] = _scope_status(tenant, gateway, "reminders")
        return Response(
            {
                "server_now": timezone.now().isoformat(),
                "window": {
                    "start_day": start_day.isoformat(),
                    "end_day_exclusive": end_day.isoformat(),
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat(),
                },
                "entity": entity,
                "items": items,
                "truncated": truncated,
                "item_limit": AGENDA_ITEM_LIMIT,
                "gateway_status": gateway.status if gateway else "unavailable",
                "scopes": scopes,
            }
        )


class RuntimeRequestCreateView(_DatebookRuntimeView):
    """Validate a create request and enter the shared human review gate."""

    throttle_classes = [DatebookRuntimeCreateThrottle]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        if not datebook_delivery_ready(tenant):
            return Response({"state": "datebook_disabled"}, status=status.HTTP_409_CONFLICT)
        if len(request.body) > RUNTIME_CREATE_BYTES:
            return Response({"state": "request_too_large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        body = request.data
        if not isinstance(body, dict):
            return Response({"state": "invalid_request"}, status=status.HTTP_400_BAD_REQUEST)
        allowed_fields = {
            "request_id",
            "command_type",
            "payload",
            "direct_user_originated",
            "originating_channel",
            "destination_name",
            "destination_fingerprint",
        }
        if not set(body).issubset(allowed_fields):
            return Response({"state": "unsupported_request_field"}, status=status.HTTP_400_BAD_REQUEST)
        request_id = body.get("request_id")
        command_type = body.get("command_type")
        direct_user_originated = body.get("direct_user_originated", False)
        originating_channel = body.get("originating_channel")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id.strip()) > 128:
            return Response({"state": "invalid_request_id"}, status=status.HTTP_400_BAD_REQUEST)
        if command_type not in DeviceCommand.CommandType.values:
            return Response({"state": "invalid_command_type"}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(direct_user_originated, bool):
            return Response({"state": "invalid_provenance"}, status=status.HTTP_400_BAD_REQUEST)
        if originating_channel not in {None, "app", "ios", "telegram", "line"}:
            return Response({"state": "invalid_originating_channel"}, status=status.HTTP_400_BAD_REQUEST)
        if originating_channel == "ios":
            originating_channel = "app"
        if command_type == DeviceCommand.CommandType.CALENDAR_CREATE:
            action_type = ActionType.CALENDAR_CREATE
            consented = bool(tenant.datebook_events_consent_at)
        else:
            action_type = ActionType.REMINDER_CREATE
            consented = bool(tenant.datebook_reminders_consent_at)
        if not consented:
            return Response({"state": "scope_not_enabled"}, status=status.HTTP_409_CONFLICT)

        try:
            payload, _item_count = _validate_command_payload(body.get("payload"), command_type=command_type)
        except ProtocolError as exc:
            return Response({"state": exc.code}, status=exc.status_code)

        record_runtime_write_activity(tenant)
        destination_name = body.get("destination_name", "")
        destination_fingerprint = body.get("destination_fingerprint", "")
        if not isinstance(destination_name, str) or len(destination_name) > 256:
            return Response({"state": "invalid_destination_name"}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(destination_fingerprint, str) or len(destination_fingerprint) > 64:
            return Response({"state": "invalid_destination_fingerprint"}, status=status.HTTP_400_BAD_REQUEST)
        display_text = _display_summary(command_type, payload)
        target_at = _first_target_at(tenant, command_type, payload)
        command_payload = {
            "request_id": request_id.strip(),
            "command_type": command_type,
            "payload": payload,
            "display_text": display_text,
            "destination_name": destination_name,
            "destination_fingerprint": destination_fingerprint,
            "target_at": target_at.isoformat() if target_at else None,
        }
        result = request_datebook_action(
            tenant,
            action_type=action_type,
            request_id=request_id.strip(),
            command_payload=command_payload,
            display_summary=display_text,
            direct_user_originated=direct_user_originated,
            originating_channel=originating_channel,
        )
        response_status = status.HTTP_202_ACCEPTED if result["state"] == "approval_pending" else status.HTTP_200_OK
        if result["state"] in {"daily_command_cap"}:
            response_status = status.HTTP_429_TOO_MANY_REQUESTS
        elif result["state"] in {"datebook_disabled", "no_active_gateway"}:
            response_status = status.HTTP_409_CONFLICT
        return Response(result, status=response_status)


class RuntimeCommandStatusView(_DatebookRuntimeView):
    """Return the narrow gate/command state used by the plugin's brief poll."""

    def get(self, request, tenant_id, command_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        sweep_device_commands(tenant=tenant)
        command = DeviceCommand.objects.filter(tenant=tenant, id=command_id).first()
        if command is not None:
            return Response(
                {
                    "command_id": str(command.id),
                    "state": command.state,
                    "execution_status": command.execution_status,
                    "mirror_status": command.mirror_status,
                    "display_text": command.display_text,
                    "datebook_command_generation": datebook_command_generation(tenant),
                }
            )
        action = PendingAction.objects.filter(tenant=tenant, datebook_command_id=command_id).first()
        if action is None:
            return Response({"state": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        result = datebook_action_state(action)
        result["datebook_command_generation"] = datebook_command_generation(tenant)
        return Response(result)
