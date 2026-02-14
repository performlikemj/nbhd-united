"""Usage dashboard API views."""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant
from .usage_serializers import DailyUsageSerializer, TransparencySerializer, UsageSummarySerializer
from .usage_services import get_daily_usage, get_transparency_data, get_usage_summary

logger = logging.getLogger(__name__)


class _TenantMixin:
    """Resolve the authenticated user's tenant."""

    def get_tenant(self, request) -> Tenant | None:
        try:
            return request.user.tenant
        except Tenant.DoesNotExist:
            return None


class UsageSummaryView(_TenantMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = self.get_tenant(request)
        if not tenant:
            return Response({"detail": "No tenant found."}, status=404)
        data = get_usage_summary(tenant)
        serializer = UsageSummarySerializer(data)
        return Response(serializer.data)


class DailyUsageView(_TenantMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = self.get_tenant(request)
        if not tenant:
            return Response({"detail": "No tenant found."}, status=404)
        days_param = request.query_params.get("days", "30")
        try:
            days = int(days_param)
        except (TypeError, ValueError):
            return Response(
                {"detail": "days must be an integer between 1 and 90."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if days < 1 or days > 90:
            return Response(
                {"detail": "days must be an integer between 1 and 90."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = get_daily_usage(tenant, days=days)
        serializer = DailyUsageSerializer(data, many=True)
        return Response(serializer.data)


class TransparencyView(_TenantMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = self.get_tenant(request)
        if not tenant:
            return Response({"detail": "No tenant found."}, status=404)
        data = get_transparency_data(tenant)
        serializer = TransparencySerializer(data)
        return Response(serializer.data)
