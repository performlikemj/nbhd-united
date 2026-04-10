"""Internal runtime views for the OpenClaw finance plugin."""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

from .models import FinanceAccount, FinanceTransaction, PayoffPlan
from .services import DebtInput, compare_strategies, calculate_payoff, payoff_result_to_dict

logger = logging.getLogger(__name__)


def _internal_auth_or_401(request, tenant_id: UUID) -> Response | None:
    try:
        validate_internal_runtime_request(
            provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
            provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
            expected_tenant_id=str(tenant_id),
        )
    except InternalAuthError as exc:
        return Response(
            {"error": "internal_auth_failed", "detail": str(exc)},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    set_rls_context(tenant_id=tenant_id, service_role=True)
    return None


def _get_tenant_or_404(tenant_id: UUID) -> Tenant | Response:
    try:
        return Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist:
        return Response(
            {"error": "tenant_not_found"},
            status=status.HTTP_404_NOT_FOUND,
        )


def _parse_decimal(value, field_name: str) -> Decimal:
    if value is None:
        raise ValueError(f"{field_name} is required")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid number") from exc


class RuntimeFinanceAccountsView(APIView):
    """GET: list accounts. POST: create/update an account."""
    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        archived_param = (request.query_params.get("archived") or "").strip().lower()
        qs = FinanceAccount.objects.filter(tenant=tenant)
        if archived_param == "true":
            qs = qs.filter(is_active=False)
        elif archived_param != "all":
            qs = qs.filter(is_active=True)

        data = [
            {
                "id": str(a.id),
                "nickname": a.nickname,
                "account_type": a.account_type,
                "current_balance": str(a.current_balance),
                "interest_rate": str(a.interest_rate) if a.interest_rate else None,
                "minimum_payment": str(a.minimum_payment) if a.minimum_payment else None,
                "credit_limit": str(a.credit_limit) if a.credit_limit else None,
                "due_day": a.due_day,
                "is_debt": a.is_debt,
                "is_active": a.is_active,
                "payoff_progress": a.payoff_progress,
            }
            for a in qs
        ]
        return Response({"accounts": data})

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        body = request.data
        nickname = (body.get("nickname") or "").strip()
        if not nickname:
            return Response(
                {"error": "nickname is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            balance = _parse_decimal(body.get("current_balance"), "current_balance")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        account_type = body.get("account_type", "other_debt")
        if account_type not in FinanceAccount.AccountType.values:
            account_type = "other_debt"

        # Upsert by nickname (fuzzy: case-insensitive)
        account, created = FinanceAccount.objects.update_or_create(
            tenant=tenant,
            nickname__iexact=nickname,
            is_active=True,
            defaults={
                "nickname": nickname,
                "account_type": account_type,
                "current_balance": balance,
                "interest_rate": _safe_decimal(body.get("interest_rate")),
                "minimum_payment": _safe_decimal(body.get("minimum_payment")),
                "credit_limit": _safe_decimal(body.get("credit_limit")),
                "due_day": _safe_int(body.get("due_day")),
            },
        )
        if created and account.original_balance is None:
            account.original_balance = balance
            account.save(update_fields=["original_balance"])

        return Response({
            "id": str(account.id),
            "nickname": account.nickname,
            "account_type": account.account_type,
            "current_balance": str(account.current_balance),
            "created": created,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class RuntimeFinanceTransactionsView(APIView):
    """POST: record a payment or transaction."""
    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        body = request.data
        nickname = (body.get("account_nickname") or "").strip()

        # Fuzzy match account by nickname
        account = None
        if nickname:
            account = FinanceAccount.objects.filter(
                tenant=tenant, is_active=True, nickname__iexact=nickname
            ).first()
            if not account:
                # Try contains match
                account = FinanceAccount.objects.filter(
                    tenant=tenant, is_active=True, nickname__icontains=nickname
                ).first()

        if not account:
            return Response(
                {"error": f"No account found matching '{nickname}'"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            amount = _parse_decimal(body.get("amount"), "amount")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        txn_type = body.get("transaction_type", "payment")
        if txn_type not in FinanceTransaction.TransactionType.values:
            txn_type = "payment"

        txn_date = body.get("date")
        if txn_date:
            try:
                txn_date = date.fromisoformat(txn_date)
            except (TypeError, ValueError):
                txn_date = date.today()
        else:
            txn_date = date.today()

        transaction = FinanceTransaction.objects.create(
            tenant=tenant,
            account=account,
            transaction_type=txn_type,
            amount=amount,
            description=(body.get("description") or "")[:256],
            date=txn_date,
        )

        # Update account balance
        if txn_type in ("payment", "refund"):
            account.current_balance = max(
                Decimal("0"), account.current_balance - amount
            )
        elif txn_type in ("charge", "interest"):
            account.current_balance += amount
        account.save(update_fields=["current_balance", "updated_at"])

        return Response({
            "transaction_id": str(transaction.id),
            "account_nickname": account.nickname,
            "new_balance": str(account.current_balance.quantize(Decimal("0.01"))),
            "transaction_type": txn_type,
            "amount": str(amount),
        }, status=status.HTTP_201_CREATED)


class RuntimeFinanceBalanceUpdateView(APIView):
    """POST: update an account balance directly."""
    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        body = request.data
        nickname = (body.get("account_nickname") or body.get("nickname") or "").strip()

        account = None
        if nickname:
            account = FinanceAccount.objects.filter(
                tenant=tenant, is_active=True, nickname__iexact=nickname
            ).first()
            if not account:
                account = FinanceAccount.objects.filter(
                    tenant=tenant, is_active=True, nickname__icontains=nickname
                ).first()

        if not account:
            return Response(
                {"error": f"No account found matching '{nickname}'"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            new_balance = _parse_decimal(body.get("new_balance"), "new_balance")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        old_balance = account.current_balance
        account.current_balance = new_balance
        account.save(update_fields=["current_balance", "updated_at"])

        return Response({
            "account_nickname": account.nickname,
            "old_balance": str(old_balance),
            "new_balance": str(account.current_balance.quantize(Decimal("0.01"))),
        })


class RuntimeFinanceArchiveAccountView(APIView):
    """POST: archive an account (soft-delete, hides from totals/calculations)."""
    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        body = request.data
        nickname = (body.get("account_nickname") or body.get("nickname") or "").strip()
        if not nickname:
            return Response(
                {"error": "account_nickname is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        account = FinanceAccount.objects.filter(
            tenant=tenant, is_active=True, nickname__iexact=nickname
        ).first()
        if not account:
            account = FinanceAccount.objects.filter(
                tenant=tenant, is_active=True, nickname__icontains=nickname
            ).first()

        if not account:
            return Response(
                {"error": f"No active account found matching '{nickname}'"},
                status=status.HTTP_404_NOT_FOUND,
            )

        previous_balance = account.current_balance
        account.is_active = False
        account.save(update_fields=["is_active", "updated_at"])

        return Response({
            "account_nickname": account.nickname,
            "archived": True,
            "previous_balance": str(previous_balance.quantize(Decimal("0.01"))),
        })


class RuntimeFinanceUnarchiveAccountView(APIView):
    """POST: restore a previously archived account."""
    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        body = request.data
        nickname = (body.get("account_nickname") or body.get("nickname") or "").strip()
        if not nickname:
            return Response(
                {"error": "account_nickname is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        account = FinanceAccount.objects.filter(
            tenant=tenant, is_active=False, nickname__iexact=nickname
        ).first()
        if not account:
            account = FinanceAccount.objects.filter(
                tenant=tenant, is_active=False, nickname__icontains=nickname
            ).first()

        if not account:
            return Response(
                {"error": f"No archived account found matching '{nickname}'"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Collision check: the upsert in RuntimeFinanceAccountsView.post() matches
        # by (tenant, nickname__iexact, is_active=True), so letting two active rows
        # share a nickname would break that path.
        collision = FinanceAccount.objects.filter(
            tenant=tenant, is_active=True, nickname__iexact=account.nickname
        ).exists()
        if collision:
            return Response(
                {
                    "error": "name_collision",
                    "detail": (
                        f"An active account named '{account.nickname}' already exists; "
                        "rename it first before restoring this one."
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )

        account.is_active = True
        account.save(update_fields=["is_active", "updated_at"])

        return Response({
            "account_nickname": account.nickname,
            "unarchived": True,
            "current_balance": str(account.current_balance.quantize(Decimal("0.01"))),
        })


class RuntimeFinancePayoffView(APIView):
    """POST: calculate and optionally save a payoff plan."""
    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        body = request.data
        try:
            monthly_budget = _parse_decimal(
                body.get("monthly_budget"), "monthly_budget"
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        strategy = body.get("strategy")  # None = compare all

        # Gather active debt accounts
        debt_accounts = FinanceAccount.objects.filter(
            tenant=tenant, is_active=True,
        ).exclude(
            account_type__in=["savings", "checking", "emergency_fund"]
        )

        debts = [
            DebtInput(
                nickname=a.nickname,
                balance=a.current_balance,
                interest_rate=a.interest_rate or Decimal("0"),
                minimum_payment=a.minimum_payment or Decimal("0"),
            )
            for a in debt_accounts
            if a.current_balance > 0
        ]

        if not debts:
            return Response({
                "message": "No active debts to calculate payoff for.",
                "results": {},
            })

        if strategy and strategy in ("snowball", "avalanche", "hybrid"):
            result = calculate_payoff(debts, monthly_budget, strategy)
            results = {strategy: payoff_result_to_dict(result)}
        else:
            all_results = compare_strategies(debts, monthly_budget)
            results = {
                k: payoff_result_to_dict(v) for k, v in all_results.items()
            }

        # Save active plan if strategy specified
        save = body.get("save", False)
        if save and strategy:
            result_data = results[strategy]
            # Deactivate existing plans
            PayoffPlan.objects.filter(tenant=tenant, is_active=True).update(
                is_active=False
            )
            PayoffPlan.objects.create(
                tenant=tenant,
                strategy=strategy,
                monthly_budget=monthly_budget,
                total_debt=Decimal(result_data["total_debt"]),
                total_interest=Decimal(result_data["total_interest"]),
                payoff_months=result_data["payoff_months"],
                payoff_date=date.fromisoformat(result_data["payoff_date"]),
                schedule_json=result_data["schedule"],
                is_active=True,
            )

        return Response({"results": results})


class RuntimeFinanceSummaryView(APIView):
    """GET: current financial overview for AI context."""
    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

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

        return Response({
            "total_debt": str(total_debt),
            "total_savings": str(total_savings),
            "total_minimum_payments": str(total_minimums),
            "debt_account_count": len(debt_accounts),
            "savings_account_count": len(savings_accounts),
            "accounts": [
                {
                    "nickname": a.nickname,
                    "account_type": a.account_type,
                    "current_balance": str(a.current_balance),
                    "interest_rate": str(a.interest_rate) if a.interest_rate else None,
                    "minimum_payment": str(a.minimum_payment) if a.minimum_payment else None,
                    "due_day": a.due_day,
                    "is_debt": a.is_debt,
                    "payoff_progress": a.payoff_progress,
                }
                for a in accounts
            ],
            "active_plan": {
                "strategy": active_plan.strategy,
                "monthly_budget": str(active_plan.monthly_budget),
                "payoff_months": active_plan.payoff_months,
                "payoff_date": active_plan.payoff_date.isoformat(),
                "total_interest": str(active_plan.total_interest),
            } if active_plan else None,
        })


def _safe_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _safe_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
