"""Tenant-facing REST API for cron job management.

Proxies requests to the tenant's OpenClaw Gateway via ``/tools/invoke``.
"""
from __future__ import annotations

import logging

from django.http import Http404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant

from .gateway_client import GatewayError, invoke_gateway_tool

logger = logging.getLogger(__name__)


def _get_tenant_for_user(user) -> Tenant:
    try:
        return user.tenant
    except Tenant.DoesNotExist as exc:
        raise Http404("Tenant not found.") from exc


def _require_active_tenant(tenant: Tenant) -> None:
    if tenant.status != Tenant.Status.ACTIVE or not tenant.container_fqdn:
        raise GatewayError("Tenant container is not active")


def _tenant_telegram_chat_id(tenant: Tenant) -> int | None:
    return getattr(tenant.user, "telegram_chat_id", None) if tenant.user_id else None


class CronJobListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)
        try:
            _require_active_tenant(tenant)
            result = invoke_gateway_tool(tenant, "cron.list", {})
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result))

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        if not data.get("name"):
            return Response(
                {"detail": "Job name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        delivery = data.get("delivery", {})
        if (
            isinstance(delivery, dict)
            and delivery.get("channel") == "telegram"
            and delivery.get("mode") != "none"
        ):
            chat_id = _tenant_telegram_chat_id(tenant)
            if chat_id and not delivery.get("to"):
                data = {**data, "delivery": {**delivery, "to": str(chat_id)}}

        try:
            _require_active_tenant(tenant)
            result = invoke_gateway_tool(tenant, "cron.add", {"job": data})
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result), status=status.HTTP_201_CREATED)


class CronJobDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, job_name: str):
        tenant = _get_tenant_for_user(request.user)
        patch_data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        delivery = patch_data.get("delivery")
        if (
            isinstance(delivery, dict)
            and delivery.get("channel") == "telegram"
            and delivery.get("mode") != "none"
        ):
            chat_id = _tenant_telegram_chat_id(tenant)
            if chat_id and not delivery.get("to"):
                patch_data = {**patch_data, "delivery": {**delivery, "to": str(chat_id)}}

        try:
            _require_active_tenant(tenant)
            logger.info("cron.update job_name=%s patch_keys=%s", job_name, list(patch_data.keys()))
            result = invoke_gateway_tool(
                tenant, "cron.update", {"jobId": job_name, "patch": patch_data},
            )
            logger.info("cron.update success job_name=%s", job_name)
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result))

    def delete(self, request, job_name: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            _require_active_tenant(tenant)
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_name})
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CronJobToggleView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, job_name: str):
        tenant = _get_tenant_for_user(request.user)

        enabled = request.data.get("enabled")
        if enabled is None:
            return Response(
                {"detail": "'enabled' field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            _require_active_tenant(tenant)
            result = invoke_gateway_tool(
                tenant,
                "cron.update",
                {"jobId": job_name, "patch": {"enabled": bool(enabled)}},
            )
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result))
