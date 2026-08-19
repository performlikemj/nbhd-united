"""Internal runtime views for the OpenClaw finance plugin."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from dateutil.relativedelta import relativedelta
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.tenant_tz import tenant_today
from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.pii.egress import KnownValueResponseGuardMixin
from apps.platform_logs.telemetry import emit_tool_event
from apps.router.document_write_guard import assert_write_allowed_for_document_turn
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

from .contracts import FinanceInputError
from .models import FinanceAccount, PayoffPlan
from .services import (
    AccountNotFound,
    DebtInput,
    calculate_payoff,
    compare_strategies,
    payoff_result_to_dict,
    record_transaction,
    resolve_account,
)

logger = logging.getLogger(__name__)

# Namespace for every enrichment event in this app. The detail keys are
# allowlisted in apps/platform_logs/telemetry.py; anything else is dropped at
# write time, which is why account nicknames and balances cannot leak here.
TELEMETRY_NAMESPACE = "finance"

DUE_DAY_MIN = 1
DUE_DAY_MAX = 31
APR_MIN = Decimal("0")
APR_MAX = Decimal("100")
# The DB column is DecimalField(max_digits=5, decimal_places=2): a third decimal
# place is not storable, so accepting one means storing a number nobody sent.
APR_MAX_DECIMAL_PLACES = 2
# max_digits on the money columns. A value past these quantizes to "too many
# digits" inside Django's DecimalField and surfaces as a 500 rather than a 400.
MAX_MONEY = Decimal("9999999999.99")  # 12,2 — current_balance, credit_limit
MAX_MINIMUM_PAYMENT = Decimal("99999999.99")  # 10,2

_MISSING = object()


class _FinanceResponseGuard(KnownValueResponseGuardMixin):
    pii_egress_seam = "finance_runtime_response"
    pii_egress_text_fields = frozenset(
        {
            "nickname",
            "description",
            "display_name",
            "account_name",
            "notes",
            "summary",
            # Candidate nicknames echoed by an ambiguous-match rejection.
            "candidates",
        }
    )


def _emit(tool_name: str, tenant_id, *, outcome: str, reason_code: str, detail: dict | None = None) -> None:
    """Record one finance enrichment event. Fail-open, content-free by construction."""
    emit_tool_event(
        namespace=TELEMETRY_NAMESPACE,
        tool_name=tool_name,
        tenant_id=tenant_id,
        outcome=outcome,
        reason_code=reason_code,
        detail=detail or {},
    )


def _reject(exc: FinanceInputError, tool_name: str, tenant_id) -> Response:
    """Turn a validation refusal into its 400 envelope plus its telemetry."""
    _emit(tool_name, tenant_id, outcome="rejected", reason_code=exc.reason_code, detail=exc.telemetry)
    return Response(exc.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)


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


def _parse_account_type(value, *, required: bool) -> str | None:
    """Validate ``account_type``, or raise.

    Unknown values used to fall back to ``other_debt``: the user's savings
    account was silently filed as a debt and counted against them in every total
    from then on. An omitted type on create is now equally loud — the plugin
    declares it required, so silence there means the call is malformed.
    """
    account_type = str(value or "").strip().lower()
    if not account_type:
        if required:
            raise FinanceInputError(
                "account_type_required",
                "account_type is required when creating an account. Pick the closest value from `allowed`.",
                field="account_type",
                reason_code="account_type_missing",
                allowed=sorted(FinanceAccount.AccountType.values),
            )
        return None
    if account_type not in FinanceAccount.AccountType.values:
        raise FinanceInputError(
            "unknown_account_type",
            "That account_type is not recognised. Use one of `allowed` — do not guess a debt type.",
            field="account_type",
            reason_code="unknown_account_type",
            allowed=sorted(FinanceAccount.AccountType.values),
        )
    return account_type


def _parse_due_day(value) -> int | None:
    """Validate ``due_day`` as a real day-of-month, or raise.

    Out-of-range values are not cosmetic. ``due_day=0`` reaches
    ``date(year, month, 0)`` in the journal status projection and raises inside
    the provider, which drops the tenant's ENTIRE finance status from the
    snapshot; the USER.md due-date window reads it modulo 31 and invents a due
    date. Both consumers are guarded too, but the value never gets in from here.
    """
    if value is None or value == "":
        return None
    try:
        day = int(str(value).strip())
    except (TypeError, ValueError):
        raise FinanceInputError(
            "due_day_invalid",
            "due_day must be a whole number between 1 and 31.",
            field="due_day",
            reason_code="due_day_out_of_range",
            telemetry={"bound": "unparseable"},
        ) from None
    if day < DUE_DAY_MIN or day > DUE_DAY_MAX:
        raise FinanceInputError(
            "due_day_out_of_range",
            (
                f"due_day must be between {DUE_DAY_MIN} and {DUE_DAY_MAX}. If the user has no fixed "
                "due date, omit the field instead of sending 0."
            ),
            field="due_day",
            reason_code="due_day_out_of_range",
            telemetry={"bound": "low" if day < DUE_DAY_MIN else "high"},
        )
    return day


def _parse_interest_rate(value) -> Decimal | None:
    """Validate an APR as a PERCENTAGE, or raise.

    Three failure modes, all previously silent: an unparseable rate became NULL
    (an unknown APR, which the payoff engine then treats as 0% and reports a
    payoff date that is too optimistic); a rate of 1000 or more parsed fine and
    then blew up as a 500 inside the DecimalField; and a fractional rate like
    ``0.229`` for "22.9%" stored a rate 100× too small after being rounded to
    two places. NULL stays available for a genuinely unknown APR — but only by
    omitting the field or sending null, never as the residue of a bad parse.
    """
    if value is None or value == "":
        return None
    try:
        rate = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise FinanceInputError(
            "apr_unparseable",
            "interest_rate must be a number, e.g. 22.9 for 22.9% APR. Omit it if the APR is unknown.",
            field="interest_rate",
            reason_code="apr_unparseable",
            telemetry={"bound": "unparseable"},
        ) from None
    if not rate.is_finite():
        raise FinanceInputError(
            "apr_unparseable",
            "interest_rate must be a finite number, e.g. 22.9 for 22.9% APR.",
            field="interest_rate",
            reason_code="apr_unparseable",
            telemetry={"bound": "unparseable"},
        )
    if rate < APR_MIN or rate > APR_MAX:
        raise FinanceInputError(
            "apr_out_of_band",
            (f"interest_rate is an annual percentage between {APR_MIN} and {APR_MAX}. Send 22.9 for 22.9% APR."),
            field="interest_rate",
            reason_code="apr_out_of_band",
            telemetry={"bound": "low" if rate < APR_MIN else "high"},
        )
    if rate.as_tuple().exponent < -APR_MAX_DECIMAL_PLACES:
        raise FinanceInputError(
            "apr_precision",
            (
                "interest_rate stores at most 2 decimal places. If you meant 22.9%, send 22.9 — "
                "not 0.229, which would record a rate 100x too small."
            ),
            field="interest_rate",
            reason_code="apr_precision",
            telemetry={"bound": "precision"},
        )
    return rate


def _parse_money(value, *, field: str, maximum: Decimal) -> Decimal | None:
    """Validate one money field, or raise. ``None``/``""`` clears it."""
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise FinanceInputError(
            "invalid_number",
            f"{field} must be a number in dollars, e.g. 1200.50.",
            field=field,
            reason_code="invalid_number",
            telemetry={"bound": "unparseable"},
        ) from None
    if not amount.is_finite():
        raise FinanceInputError(
            "invalid_number",
            f"{field} must be a finite number in dollars.",
            field=field,
            reason_code="invalid_number",
            telemetry={"bound": "unparseable"},
        )
    if abs(amount) > maximum:
        raise FinanceInputError(
            "amount_out_of_range",
            f"{field} is larger than this ledger can store (max {maximum}). Check the units.",
            field=field,
            reason_code="amount_out_of_range",
            telemetry={"bound": "high"},
        )
    return amount


class RuntimeFinanceAccountsView(_FinanceResponseGuard, APIView):
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

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        body = request.data
        tool = "runtime-finance-accounts"
        nickname = (body.get("nickname") or "").strip()
        if not nickname:
            return _reject(
                FinanceInputError(
                    "nickname_required",
                    "nickname is required — it is how the user refers to the account.",
                    field="nickname",
                ),
                tool,
                tenant_id,
            )

        # Fields the caller actually sent. This is the whole fix for the update
        # path: the old code read every optional field with .get() and wrote the
        # resulting None straight into the row, so "update the balance" wiped the
        # APR, the minimum payment, the credit limit and the due day. An omitted
        # field now means "leave it alone"; an explicit null still clears it.
        optional_parsers = (
            ("interest_rate", _parse_interest_rate),
            ("minimum_payment", lambda v: _parse_money(v, field="minimum_payment", maximum=MAX_MINIMUM_PAYMENT)),
            ("credit_limit", lambda v: _parse_money(v, field="credit_limit", maximum=MAX_MONEY)),
            ("due_day", _parse_due_day),
        )
        supplied: dict = {}
        try:
            for field, parser in optional_parsers:
                raw = body.get(field, _MISSING)
                if raw is not _MISSING:
                    supplied[field] = parser(raw)

            raw_balance = body.get("current_balance", _MISSING)
            if raw_balance is not _MISSING:
                balance = _parse_money(raw_balance, field="current_balance", maximum=MAX_MONEY)
                # Unlike the optional columns, a balance has no "unknown" state:
                # an explicit null is a malformed instruction, not a clear.
                if balance is None:
                    raise FinanceInputError(
                        "current_balance_required",
                        "current_balance cannot be null. Omit it to leave the stored balance alone.",
                        field="current_balance",
                    )
                supplied["current_balance"] = balance

            raw_type = body.get("account_type", _MISSING)
            if raw_type is not _MISSING:
                supplied["account_type"] = _parse_account_type(raw_type, required=False)
        except FinanceInputError as exc:
            return _reject(exc, tool, tenant_id)

        from apps.pii.store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {"nickname": nickname},
            model_label="finance.FinanceAccount",
            seam="finance.runtime.account.upsert",
            writer="runtime",
            defer_detection=True,
        )
        stored_nickname = authored["nickname"]

        # Upsert by placeholder-space nickname (fuzzy: case-insensitive)
        account = FinanceAccount.objects.filter(
            tenant=tenant,
            is_active=True,
            nickname__iexact=stored_nickname,  # guard: encrypted-predicate
        ).first()
        created = account is None

        account_type = supplied.get("account_type")
        try:
            if created:
                # Create still demands the fields that define the account: a row
                # with no type and no balance is not a partial update, it is a
                # malformed call.
                account_type = _parse_account_type(account_type, required=True)
                if supplied.get("current_balance") is None:
                    raise FinanceInputError(
                        "current_balance_required",
                        "current_balance is required when creating an account.",
                        field="current_balance",
                    )
            elif account_type is None:
                # An explicit null account_type on update would blank the column;
                # there is no such thing as a typeless account, so ignore it.
                supplied.pop("account_type", None)
        except FinanceInputError as exc:
            return _reject(exc, tool, tenant_id)

        if created:
            balance = supplied["current_balance"]
            account = FinanceAccount.objects.create(
                tenant=tenant,
                nickname=stored_nickname,
                pii_receipts=receipts,
                account_type=account_type,
                current_balance=balance,
                original_balance=balance,
                interest_rate=supplied.get("interest_rate"),
                minimum_payment=supplied.get("minimum_payment"),
                credit_limit=supplied.get("credit_limit"),
                due_day=supplied.get("due_day"),
            )
        else:
            account.nickname = stored_nickname
            account.pii_receipts = receipts
            for field, value in supplied.items():
                setattr(account, field, value)
            account.save(
                update_fields=["nickname", "pii_receipts", *supplied.keys(), "updated_at"],
            )
            _emit(
                tool,
                tenant_id,
                outcome="accepted",
                reason_code="partial_update_fields",
                detail={"field_count": len(supplied)},
            )

        return Response(
            {
                "id": str(account.id),
                "nickname": account.nickname,
                "account_type": account.account_type,
                "current_balance": str(account.current_balance),
                "created": created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class RuntimeFinanceTransactionsView(_FinanceResponseGuard, APIView):
    """POST: record a payment or transaction."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        body = request.data
        tool = "runtime-finance-transactions"
        try:
            account = resolve_account(tenant, account_nickname=body.get("account_nickname"))
        except FinanceInputError as exc:
            return _reject(exc, tool, tenant_id)
        except AccountNotFound as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)

        try:
            amount = _parse_decimal(body.get("amount"), "amount")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # The tenant's calendar day, not the server's. The dedup key includes the
        # date, so a UTC "today" made a retry that crossed 00:00 UTC (09:00 JST)
        # look like a brand-new payment and debited the account a second time.
        today = tenant_today(tenant)
        raw_date = body.get("date")
        date_source = "tenant_today"
        if raw_date:
            try:
                txn_date = date.fromisoformat(raw_date)
                date_source = "body"
            except (TypeError, ValueError):
                txn_date = today
        else:
            txn_date = today
        if date_source == "tenant_today":
            _emit(
                tool,
                tenant_id,
                outcome="normalized",
                reason_code="tenant_date_applied",
                detail={"date_source": date_source},
            )

        try:
            payload, created = record_transaction(
                tenant=tenant,
                account=account,
                amount=amount,
                transaction_type=body.get("transaction_type", "payment"),
                txn_date=txn_date,
                description=body.get("description") or "",
                writer="runtime",
            )
        except FinanceInputError as exc:
            return _reject(exc, tool, tenant_id)

        if created and payload.get("transaction_type") == "deposit":
            _emit(tool, tenant_id, outcome="accepted", reason_code="deposit_recorded", detail={"txn_type": "deposit"})

        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class RuntimeFinanceBalanceUpdateView(_FinanceResponseGuard, APIView):
    """POST: update an account balance directly."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        body = request.data
        tool = "runtime-finance-balance"
        nickname = (body.get("account_nickname") or body.get("nickname") or "").strip()

        # Shared resolution: this path used to keep its own copy of the fuzzy
        # match, so it inherited the same sticky first()-wins pick and skipped
        # the placeholder-space variants the rest of finance searches on.
        try:
            account = resolve_account(tenant, account_nickname=nickname)
        except FinanceInputError as exc:
            return _reject(exc, tool, tenant_id)
        except AccountNotFound:
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

        return Response(
            {
                "account_nickname": account.nickname,
                "old_balance": str(old_balance),
                "new_balance": str(account.current_balance.quantize(Decimal("0.01"))),
            }
        )


class RuntimeFinanceArchiveAccountView(_FinanceResponseGuard, APIView):
    """POST: archive an account (soft-delete, hides from totals/calculations)."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        body = request.data
        nickname = (body.get("account_nickname") or body.get("nickname") or "").strip()
        if not nickname:
            return Response(
                {"error": "account_nickname is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            account = resolve_account(tenant, account_nickname=nickname)
        except FinanceInputError as exc:
            return _reject(exc, "runtime-finance-archive-account", tenant_id)
        except AccountNotFound:
            return Response(
                {"error": f"No active account found matching '{nickname}'"},
                status=status.HTTP_404_NOT_FOUND,
            )

        previous_balance = account.current_balance
        account.is_active = False
        account.save(update_fields=["is_active", "updated_at"])

        return Response(
            {
                "account_nickname": account.nickname,
                "archived": True,
                "previous_balance": str(previous_balance.quantize(Decimal("0.01"))),
            }
        )


class RuntimeFinanceUnarchiveAccountView(_FinanceResponseGuard, APIView):
    """POST: restore a previously archived account."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        body = request.data
        nickname = (body.get("account_nickname") or body.get("nickname") or "").strip()
        if not nickname:
            return Response(
                {"error": "account_nickname is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            account = resolve_account(tenant, account_nickname=nickname, is_active=False)
        except FinanceInputError as exc:
            return _reject(exc, "runtime-finance-unarchive-account", tenant_id)
        except AccountNotFound:
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

        return Response(
            {
                "account_nickname": account.nickname,
                "unarchived": True,
                "current_balance": str(account.current_balance.quantize(Decimal("0.01"))),
            }
        )


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

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        body = request.data
        try:
            monthly_budget = _parse_decimal(body.get("monthly_budget"), "monthly_budget")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        strategy = body.get("strategy")  # None = compare all

        _VALID_STRATEGIES = ("snowball", "avalanche", "hybrid")
        if strategy is not None and strategy not in _VALID_STRATEGIES:
            return Response(
                {"error": f"Unknown strategy: {strategy!r}. Must be one of: snowball, avalanche, hybrid"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Gather active debt accounts
        debt_accounts = FinanceAccount.objects.filter(
            tenant=tenant,
            is_active=True,
        ).exclude(account_type__in=["savings", "checking", "emergency_fund"])

        scored = [a for a in debt_accounts if a.current_balance > 0]
        debts = [
            DebtInput(
                nickname=a.nickname,
                balance=a.current_balance,
                interest_rate=a.interest_rate or Decimal("0"),
                minimum_payment=a.minimum_payment or Decimal("0"),
            )
            for a in scored
        ]

        if not debts:
            return Response(
                {
                    "message": "No active debts to calculate payoff for.",
                    "results": {},
                }
            )

        # A NULL APR is treated as 0% by the engine above, which quietly makes the
        # projected payoff date earlier than reality. The math is unchanged here
        # (that is a separate decision); this records how often the substitution
        # fires so the size of the lie is measurable rather than assumed.
        null_apr_count = sum(1 for a in scored if a.interest_rate is None)
        if null_apr_count:
            _emit(
                "runtime-finance-payoff",
                tenant_id,
                outcome="normalized",
                reason_code="null_apr_as_zero",
                detail={"field_count": null_apr_count},
            )

        # Payoff schedules start next month in the TENANT's calendar, not the
        # server's — the stored payoff_date is a user-facing promise.
        start_date = (tenant_today(tenant) + relativedelta(months=1)).replace(day=1)

        if strategy and strategy in ("snowball", "avalanche", "hybrid"):
            result = calculate_payoff(debts, monthly_budget, strategy, start_date)
            results = {strategy: payoff_result_to_dict(result)}
        else:
            all_results = compare_strategies(debts, monthly_budget, start_date)
            results = {k: payoff_result_to_dict(v) for k, v in all_results.items()}

        # Save active plan if strategy specified
        save = body.get("save", False)
        if save and strategy:
            result_data = results[strategy]
            from apps.pii.store_authoring import author_store_fields

            authored, receipts = author_store_fields(
                tenant,
                {"schedule_json": result_data["schedule"]},
                model_label="finance.PayoffPlan",
                seam="finance.runtime.payoff_plan",
                writer="runtime",
                defer_detection=True,
            )
            # Deactivate existing plans
            PayoffPlan.objects.filter(tenant=tenant, is_active=True).update(is_active=False)
            PayoffPlan.objects.create(
                tenant=tenant,
                strategy=strategy,
                monthly_budget=monthly_budget,
                total_debt=Decimal(result_data["total_debt"]),
                total_interest=Decimal(result_data["total_interest"]),
                payoff_months=result_data["payoff_months"],
                payoff_date=date.fromisoformat(result_data["payoff_date"]),
                schedule_json=authored["schedule_json"],
                pii_receipts=receipts,
                is_active=True,
            )

        return Response({"results": results})


class RuntimeFinanceSummaryView(_FinanceResponseGuard, APIView):
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
        total_minimums = sum(a.minimum_payment for a in debt_accounts if a.minimum_payment) or Decimal("0")

        active_plan = PayoffPlan.objects.filter(tenant=tenant, is_active=True).first()

        return Response(
            {
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
                }
                if active_plan
                else None,
            }
        )
