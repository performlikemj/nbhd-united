"""Consumer-facing finance API views (JWT auth, frontend)."""
from decimal import Decimal

from django.db.models import Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FinanceAccount, FinanceSnapshot, FinanceTransaction, PayoffPlan
from .serializers import (
    FinanceAccountSerializer,
    FinanceDashboardSerializer,
    FinanceSnapshotSerializer,
    FinanceTransactionSerializer,
    PayoffPlanSerializer,
)


class FinanceAccountListView(APIView):
    """GET: list accounts. POST: create account."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        accounts = FinanceAccount.objects.filter(tenant=tenant, is_active=True)
        serializer = FinanceAccountSerializer(accounts, many=True)
        return Response(serializer.data)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        serializer = FinanceAccountSerializer(
            data=request.data, context={"tenant": tenant}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class FinanceAccountDetailView(APIView):
    """PATCH: update account. DELETE: soft-delete account."""
    permission_classes = [IsAuthenticated]

    def _get_account(self, request, account_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return None, Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            account = FinanceAccount.objects.get(id=account_id, tenant=tenant)
            return account, None
        except FinanceAccount.DoesNotExist:
            return None, Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

    def patch(self, request, account_id):
        account, error = self._get_account(request, account_id)
        if error:
            return error
        serializer = FinanceAccountSerializer(
            account, data=request.data, partial=True,
            context={"tenant": account.tenant},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, account_id):
        account, error = self._get_account(request, account_id)
        if error:
            return error
        account.is_active = False
        account.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class FinanceTransactionListView(APIView):
    """GET: list recent transactions."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        transactions = FinanceTransaction.objects.filter(
            tenant=tenant
        ).select_related("account")[:50]
        serializer = FinanceTransactionSerializer(transactions, many=True)
        return Response(serializer.data)


class PayoffPlanListView(APIView):
    """GET: list payoff plans."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        plans = PayoffPlan.objects.filter(tenant=tenant)
        serializer = PayoffPlanSerializer(plans, many=True)
        return Response(serializer.data)


class FinanceSnapshotListView(APIView):
    """GET: list monthly snapshots."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        snapshots = FinanceSnapshot.objects.filter(tenant=tenant)[:24]
        serializer = FinanceSnapshotSerializer(snapshots, many=True)
        return Response(serializer.data)


class FinanceDashboardView(APIView):
    """GET: aggregated finance summary for the dashboard tab."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        accounts = list(FinanceAccount.objects.filter(tenant=tenant, is_active=True))
        debt_types = FinanceAccount.DEBT_TYPES
        debt_accounts = [a for a in accounts if a.account_type in debt_types]
        savings_accounts = [a for a in accounts if a.account_type not in debt_types]

        total_debt = sum(a.current_balance for a in debt_accounts) or Decimal("0")
        total_savings = sum(a.current_balance for a in savings_accounts) or Decimal("0")
        total_minimums = sum(
            a.minimum_payment for a in debt_accounts if a.minimum_payment
        ) or Decimal("0")

        active_plan = PayoffPlan.objects.filter(
            tenant=tenant, is_active=True
        ).first()

        snapshots = list(FinanceSnapshot.objects.filter(tenant=tenant)[:12])

        recent_transactions = list(
            FinanceTransaction.objects.filter(tenant=tenant)
            .select_related("account")[:10]
        )

        data = {
            "total_debt": total_debt,
            "total_savings": total_savings,
            "total_minimum_payments": total_minimums,
            "debt_account_count": len(debt_accounts),
            "savings_account_count": len(savings_accounts),
            "accounts": accounts,
            "active_plan": active_plan,
            "snapshots": snapshots,
            "recent_transactions": recent_transactions,
        }
        serializer = FinanceDashboardSerializer(data)
        return Response(serializer.data)


class FinanceSettingsView(APIView):
    """PATCH: toggle finance_enabled for the tenant."""
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        finance_enabled = request.data.get("finance_enabled")
        if finance_enabled is None:
            return Response(
                {"error": "finance_enabled is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        tenant.finance_enabled = bool(finance_enabled)
        tenant.save(update_fields=["finance_enabled"])
        tenant.bump_pending_config()
        return Response({"finance_enabled": tenant.finance_enabled})
