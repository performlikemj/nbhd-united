"""User-facing Journal API views."""
from __future__ import annotations

import datetime
from uuid import UUID

from django.http import Http404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant

from .models import JournalEntry
from .serializers import JournalEntrySerializer


def _get_tenant_for_user(user) -> Tenant:
    try:
        return user.tenant
    except Tenant.DoesNotExist as exc:
        raise Http404("Tenant not found.") from exc


def _get_entry_for_tenant(*, tenant: Tenant, entry_id: UUID) -> JournalEntry:
    try:
        return JournalEntry.objects.get(id=entry_id, tenant=tenant)
    except JournalEntry.DoesNotExist as exc:
        raise Http404("Journal entry not found.") from exc


class JournalEntryListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)
        queryset = JournalEntry.objects.filter(tenant=tenant).order_by("-date", "-created_at")

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            try:
                queryset = queryset.filter(date__gte=datetime.date.fromisoformat(date_from))
            except ValueError:
                pass
        if date_to:
            try:
                queryset = queryset.filter(date__lte=datetime.date.fromisoformat(date_to))
            except ValueError:
                pass

        serializer = JournalEntrySerializer(queryset, many=True)
        return Response(serializer.data)

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)
        serializer = JournalEntrySerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class JournalEntryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, entry_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        entry = _get_entry_for_tenant(tenant=tenant, entry_id=entry_id)
        return Response(JournalEntrySerializer(entry).data)

    def patch(self, request, entry_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        entry = _get_entry_for_tenant(tenant=tenant, entry_id=entry_id)
        serializer = JournalEntrySerializer(entry, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        return Response(JournalEntrySerializer(updated).data)

    def delete(self, request, entry_id: UUID):
        tenant = _get_tenant_for_user(request.user)
        entry = _get_entry_for_tenant(tenant=tenant, entry_id=entry_id)
        entry.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
