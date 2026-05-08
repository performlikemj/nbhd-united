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


_FINANCE_WELCOME_PROMPT_TEMPLATE = (
    "Gravity (the user's finance-tracking module) was just enabled. Send them "
    "a brief, warm welcome via `nbhd_send_to_user` letting them know their "
    "finance assistant is ready. Keep it to 2-3 sentences — mention you can "
    "help track debts and savings, build payoff strategies (snowball / "
    "avalanche / hybrid), and surface upcoming due dates. Invite them to "
    "share what they'd like to start with whenever they're ready. Don't "
    "start a full questionnaire in this message — just open the door.\n\n"
    "**Do NOT ask questions in this message.** Just welcome them and let "
    "them know you're here when they want to set things up.\n\n"
    "**After `nbhd_send_to_user` succeeds**, mark the welcome as delivered "
    "so the deploy-time backfill knows not to re-send. Run via Bash:\n"
    "  curl -fsS -X POST \\\n"
    '    "$NBHD_API_BASE_URL/api/v1/tenants/runtime/{tenant_id}/welcomes/finance/" \\\n'
    '    -H "X-NBHD-Internal-Key: $NBHD_INTERNAL_API_KEY" \\\n'
    '    -H "X-NBHD-Tenant-Id: {tenant_id}"\n\n'
    "If `nbhd_send_to_user` returned an error (timeout, channel rejection, "
    "etc.), DO NOT run the curl — leave the welcome unmarked so the next "
    "deploy's backfill retries."
)


# Backwards-compat alias for ``_KNOWN_DEFAULT_PREFIXES``-style heuristics.
_FINANCE_WELCOME_PROMPT = _FINANCE_WELCOME_PROMPT_TEMPLATE


def _schedule_finance_welcome(tenant):
    """Create a one-shot cron that sends a Gravity welcome message.

    Fires ~5 minutes after enablement. Self-healing: a previously stale
    welcome cron (date already passed without successful self-removal)
    is replaced.

    Returns the ``WelcomeStatus`` enum. Raises on transport failure;
    callers that want fire-and-forget semantics should wrap.
    """
    from apps.orchestrator.welcome_scheduler import schedule_welcome

    return schedule_welcome(
        tenant,
        feature="finance",
        cron_name="_finance:welcome",
        prompt_template=_FINANCE_WELCOME_PROMPT_TEMPLATE,
    )


class FinanceSettingsView(APIView):
    """PATCH: toggle finance_enabled for the tenant.

    Enabling or disabling Gravity flips the ``nbhd-finance-tools`` plugin
    in the OpenClaw config allow-list. The running session's tool manifest
    is built once at session start, so a hot-reload of the file isn't
    enough — the container must restart for the agent to actually see the
    new tools (or stop seeing them on disable). Mirrors the Fuel toggle:
    we queue an ``apply_single_tenant_config`` so the file share is current
    before the restart, then return ``restart_required`` so the frontend
    can confirm and call ``POST /api/v1/finance/restart/``. The welcome
    cron is scheduled from the restart endpoint, AFTER the container has
    had time to come back up with the new plugin loaded.
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
        update_fields = ["finance_enabled"]

        # On a fresh enable (off → on), clear any previous delivery
        # marker so the welcome re-fires. This is the supported path for
        # users who want to re-experience the welcome — disable, then
        # re-enable. The delivery-tracking idempotency check would
        # otherwise skip them.
        if tenant.finance_enabled and not was_enabled:
            marks = dict(tenant.welcomes_sent or {})
            if marks.pop("finance", None) is not None:
                tenant.welcomes_sent = marks
                update_fields.append("welcomes_sent")

        tenant.save(update_fields=update_fields)
        tenant.bump_pending_config()

        # Write config to file share so it's ready when the container restarts.
        try:
            from apps.cron.publish import publish_task

            publish_task("apply_single_tenant_config", str(tenant.id))
        except Exception:
            logger.warning("Failed to enqueue config deploy for tenant %s", tenant.id)

        plugin_changed = was_enabled != tenant.finance_enabled
        restart_required = plugin_changed and bool(tenant.container_id)

        return Response(
            {
                "finance_enabled": tenant.finance_enabled,
                "restart_required": restart_required,
            }
        )


class FinanceRestartView(APIView):
    """POST: restart the assistant to pick up Gravity plugin changes.

    Called after the user confirms the restart in the frontend. Mirrors
    ``FuelRestartView`` — ``restart_container_app`` mints a new revision
    that reads the latest ``openclaw.json`` from the file share at boot,
    then we schedule the welcome cron 90s out so the cold-started
    container has time to be reachable when the cron fires.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        if not tenant.container_id:
            return Response({"error": "no_container"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from apps.orchestrator.azure_client import restart_container_app

            restart_container_app(tenant.container_id)
        except Exception:
            logger.exception("Container restart failed for tenant %s", tenant.id)
            return Response(
                {"error": "restart_failed", "detail": "Could not restart your assistant. Try again in a moment."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Schedule welcome cron AFTER the container is back up (~90s for
        # cold start). We can't call the Gateway API on a restarting container.
        if tenant.finance_enabled:
            try:
                from apps.cron.publish import publish_task

                publish_task(
                    "schedule_finance_welcome",
                    str(tenant.id),
                    delay_seconds=90,
                )
            except Exception:
                logger.warning("Failed to enqueue finance welcome for tenant %s", tenant.id)

        return Response({"restarted": True})
