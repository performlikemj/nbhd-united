"""Automation API views."""
from __future__ import annotations

from uuid import UUID

from django.http import Http404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant

from .models import Automation, AutomationRun
from .serializers import AutomationRunSerializer, AutomationSerializer
from .services import (
    AutomationLimitError,
    pause_automation,
    resume_automation,
    execute_automation,
)


def _get_tenant_for_user(user) -> Tenant:
    try:
        return user.tenant
    except Tenant.DoesNotExist as exc:
        raise Http404("Tenant not found.") from exc


def _get_automation_for_tenant(*, tenant: Tenant, automation_id: UUID) -> Automation:
    try:
        return Automation.objects.select_related("tenant", "tenant__user").get(
            id=automation_id,
            tenant=tenant,
        )
    except Automation.DoesNotExist as exc:
        raise Http404("Automation not found.") from exc


class AutomationListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)
        automations = Automation.objects.filter(tenant=tenant).order_by("created_at")
        serializer = AutomationSerializer(automations, many=True)
        return Response(serializer.data)

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)
        serializer = AutomationSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        automation = serializer.save()
        return Response(AutomationSerializer(automation).data, status=status.HTTP_201_CREATED)


class AutomationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, automation_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        automation = _get_automation_for_tenant(tenant=tenant, automation_id=automation_id)
        return Response(AutomationSerializer(automation).data)

    def patch(self, request, automation_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        automation = _get_automation_for_tenant(tenant=tenant, automation_id=automation_id)
        serializer = AutomationSerializer(automation, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        return Response(AutomationSerializer(updated).data)

    def delete(self, request, automation_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        automation = _get_automation_for_tenant(tenant=tenant, automation_id=automation_id)
        automation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AutomationPauseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, automation_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        automation = _get_automation_for_tenant(tenant=tenant, automation_id=automation_id)
        paused = pause_automation(automation)
        return Response(AutomationSerializer(paused).data)


class AutomationResumeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, automation_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        automation = _get_automation_for_tenant(tenant=tenant, automation_id=automation_id)
        try:
            resumed = resume_automation(automation)
        except AutomationLimitError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AutomationSerializer(resumed).data)


class AutomationManualRunView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, automation_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        automation = _get_automation_for_tenant(tenant=tenant, automation_id=automation_id)
        try:
            run = execute_automation(
                automation=automation,
                trigger_source=AutomationRun.TriggerSource.MANUAL,
            )
        except AutomationLimitError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AutomationRunSerializer(run).data, status=status.HTTP_201_CREATED)


class AutomationRunsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, automation_id: UUID | None = None):
        tenant = _get_tenant_for_user(request.user)
        queryset = AutomationRun.objects.filter(tenant=tenant).select_related("automation").order_by("-created_at")

        if automation_id is not None:
            automation = _get_automation_for_tenant(tenant=tenant, automation_id=automation_id)
            queryset = queryset.filter(automation=automation)

        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = AutomationRunSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)
