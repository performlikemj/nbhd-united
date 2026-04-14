"""Usage dashboard API views."""

import logging

from rest_framework import serializers, status
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


class _DonationPreferenceSerializer(serializers.Serializer):
    donation_enabled = serializers.BooleanField(required=False)
    donation_percentage = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=100,
    )


class DonationPreferenceView(_TenantMixin, APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        tenant = self.get_tenant(request)
        if not tenant:
            return Response({"detail": "No tenant found."}, status=404)
        serializer = _DonationPreferenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = False
        if "donation_enabled" in serializer.validated_data:
            tenant.donation_enabled = serializer.validated_data["donation_enabled"]
            updated = True
        if "donation_percentage" in serializer.validated_data:
            tenant.donation_percentage = serializer.validated_data["donation_percentage"]
            updated = True
        if updated:
            tenant.save(update_fields=["donation_enabled", "donation_percentage"])
        return Response(
            {
                "donation_enabled": tenant.donation_enabled,
                "donation_percentage": tenant.donation_percentage,
            }
        )
