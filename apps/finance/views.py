"""Consumer-facing finance API views (JWT auth, frontend)."""

import logging
from decimal import Decimal

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FinanceAccount, FinanceSnapshot, FinanceTransaction, PayoffPlan

logger = logging.getLogger(__name__)
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
        archived_param = (request.query_params.get("archived") or "").strip().lower()
        if archived_param == "true":
            accounts = FinanceAccount.objects.filter(tenant=tenant, is_active=False)
        else:
            accounts = FinanceAccount.objects.filter(tenant=tenant, is_active=True)
        serializer = FinanceAccountSerializer(accounts, many=True)
        return Response(serializer.data)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        serializer = FinanceAccountSerializer(data=request.data, context={"tenant": tenant})
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
            account,
            data=request.data,
            partial=True,
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
        transactions = FinanceTransaction.objects.filter(tenant=tenant).select_related("account")[:50]
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
        total_minimums = sum(a.minimum_payment for a in debt_accounts if a.minimum_payment) or Decimal("0")

        active_plan = PayoffPlan.objects.filter(tenant=tenant, is_active=True).first()

        snapshots = list(FinanceSnapshot.objects.filter(tenant=tenant)[:12])

        recent_transactions = list(FinanceTransaction.objects.filter(tenant=tenant).select_related("account")[:10])

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


_FINANCE_WELCOME_PROMPT = (
    "Gravity (the user's finance-tracking module) was just enabled. Send them "
    "a brief, warm welcome via `nbhd_send_to_user` letting them know their "
    "finance assistant is ready. Keep it to 2-3 sentences — mention you can "
    "help track debts and savings, build payoff strategies (snowball / "
    "avalanche / hybrid), and surface upcoming due dates. Invite them to "
    "share what they'd like to start with whenever they're ready. Don't "
    "start a full questionnaire in this message — just open the door.\n\n"
    "**Do NOT ask questions in this message.** Just welcome them and let "
    "them know you're here when they want to set things up."
)


def _schedule_finance_welcome(tenant) -> None:
    """Create a one-shot cron that sends a Gravity welcome message.

    Fires ~5 minutes after enablement (gives the container time to pick up
    the new finance plugin if a config refresh is in flight). Mirrors the
    fuel welcome pattern in ``apps/fuel/views.py``.

    Best-effort — failures are logged, not raised. The tenant still gets
    organic onboarding on their next message.
    """
    import zoneinfo
    from datetime import datetime, timedelta

    try:
        from apps.cron.gateway_client import invoke_gateway_tool

        user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
        try:
            tz = zoneinfo.ZoneInfo(user_tz)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")

        fire_at = datetime.now(tz) + timedelta(minutes=5)
        # Date-specific cron expr fires exactly once.
        cron_expr = f"{fire_at.minute} {fire_at.hour} {fire_at.day} {fire_at.month} *"

        welcome_message = (
            _FINANCE_WELCOME_PROMPT
            + "\n\n---\n"
            + "After sending the welcome, remove this cron: `cron remove _finance:welcome`"
        )

        invoke_gateway_tool(
            tenant,
            "cron.add",
            {
                "job": {
                    "name": "_finance:welcome",
                    "schedule": {"kind": "cron", "expr": cron_expr, "tz": user_tz},
                    "sessionTarget": "isolated",
                    "payload": {
                        "kind": "agentTurn",
                        "message": welcome_message,
                    },
                    "delivery": {"mode": "none"},
                    "enabled": True,
                }
            },
        )
        logger.info("Scheduled finance welcome cron for tenant %s (fires at %s)", tenant.id, fire_at.isoformat())
    except Exception:
        logger.warning(
            "Failed to schedule finance welcome for tenant %s (user will get onboarding on next message)",
            tenant.id,
        )


class FinanceSettingsView(APIView):
    """PATCH: toggle finance_enabled for the tenant.

    Enabling Gravity (finance) schedules a one-shot welcome cron 90s out
    so the agent introduces the feature shortly after the toggle. Idempotent:
    re-enabling an already-enabled tenant doesn't re-schedule the welcome.
    """

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

        was_enabled = tenant.finance_enabled
        tenant.finance_enabled = bool(finance_enabled)
        tenant.save(update_fields=["finance_enabled"])
        tenant.bump_pending_config()

        # First-enable: schedule the welcome cron via QStash with a delay
        # so the container has a moment to pick up the latest config.
        if tenant.finance_enabled and not was_enabled:
            try:
                from apps.cron.publish import publish_task

                publish_task(
                    "schedule_finance_welcome",
                    str(tenant.id),
                    delay_seconds=90,
                )
            except Exception:
                logger.warning("Failed to enqueue finance welcome for tenant %s", tenant.id)

        return Response({"finance_enabled": tenant.finance_enabled})
