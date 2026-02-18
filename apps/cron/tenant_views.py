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

        data = request.data
        if not data.get("name"):
            return Response(
                {"detail": "Job name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            _require_active_tenant(tenant)
            result = invoke_gateway_tool(tenant, "cron.add", data)
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result), status=status.HTTP_201_CREATED)


class CronJobDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, job_name: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            _require_active_tenant(tenant)
            result = invoke_gateway_tool(
                tenant, "cron.update", {"name": job_name, **request.data},
            )
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result))

    def delete(self, request, job_name: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            _require_active_tenant(tenant)
            invoke_gateway_tool(tenant, "cron.remove", {"name": job_name})
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
                tenant, "cron.update", {"name": job_name, "enabled": bool(enabled)},
            )
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result))
