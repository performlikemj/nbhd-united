"""Dashboard API — aggregated views for the frontend."""
from __future__ import annotations

from django.db.models import Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import UsageRecord
from apps.integrations.models import Integration
from apps.orchestrator.services import check_tenant_health
from apps.tenants.models import Tenant


class DashboardView(APIView):
    """Main dashboard — tenant status, usage, connections."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response(
                {"detail": "No tenant found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Usage stats
        usage = UsageRecord.objects.filter(tenant=tenant).aggregate(
            total_input_tokens=Sum("input_tokens"),
            total_output_tokens=Sum("output_tokens"),
            total_cost=Sum("cost_estimate"),
        )

        # Connected services
        connections = list(
            Integration.objects.filter(
                tenant=tenant,
                status=Integration.Status.ACTIVE,
            ).values("provider", "provider_email", "connected_at")
        )

        # Health check
        health = check_tenant_health(str(tenant.id))

        return Response({
            "tenant": {
                "id": str(tenant.id),
                "status": tenant.status,
                "model_tier": tenant.model_tier,
                "provisioned_at": tenant.provisioned_at,
            },
            "usage": {
                "messages_today": tenant.messages_today,
                "messages_this_month": tenant.messages_this_month,
                "tokens_this_month": tenant.tokens_this_month,
                "estimated_cost_this_month": str(tenant.estimated_cost_this_month),
                "monthly_token_budget": tenant.monthly_token_budget,
                "total_input_tokens": usage["total_input_tokens"] or 0,
                "total_output_tokens": usage["total_output_tokens"] or 0,
                "total_cost": str(usage["total_cost"] or 0),
            },
            "connections": connections,
            "health": health,
        })


class UsageHistoryView(APIView):
    """Usage history — recent usage records."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        records = UsageRecord.objects.filter(tenant=tenant).order_by("-created_at")[:50]
        data = [
            {
                "id": str(r.id),
                "event_type": r.event_type,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "model_used": r.model_used,
                "cost_estimate": str(r.cost_estimate),
                "created_at": r.created_at,
            }
            for r in records
        ]
        return Response({"results": data})
