"""Parameterized query endpoint for the Gravity (finance) domain.

Single endpoint POST /api/v1/finance/runtime/<tenant_id>/query/ backed by
``FinanceQueryView``. Resources: ``accounts``, ``transactions``, ``plan``.
Companion to the existing mutation endpoints in ``runtime_views.py`` — this
module is read-only.

See ``CONTINUITY_agent-context-via-queries.md`` for the architectural
rationale and ``apps.common.query_view.BaseQueryView`` for the dispatch
contract (auth, RLS, window resolution, hashing, meta envelope).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Literal

from django.db.models import Avg, Count, Max, Min, QuerySet, Sum
from pydantic import BaseModel, ConfigDict, Field

from apps.common.query_view import BaseQueryView, QueryExecutionError, jsonify
from apps.common.windows import Window
from apps.finance.models import FinanceAccount, FinanceTransaction, PayoffPlan
from apps.tenants.models import Tenant

# ─── Request schema ────────────────────────────────────────────────────────


FinanceResource = Literal["accounts", "transactions", "plan"]
Aggregate = Literal["sum", "count", "avg", "min", "max"]


class FinanceQueryRequest(BaseModel):
    """Strict request shape for ``nbhd_gravity_query``.

    ``extra="forbid"`` so typos in filter or field names surface as 400 rather
    than silent no-ops. Per-resource filter validation happens in the view.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    resource: FinanceResource
    window: Window | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    fields: list[str] | None = None
    aggregate: Aggregate | None = None
    aggregate_field: str | None = None
    group_by: str | None = None
    order_by: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


# ─── Field catalogues + filter handlers ────────────────────────────────────


_AGG_MAP = {"sum": Sum, "count": Count, "avg": Avg, "min": Min, "max": Max}

# Identifier always included in row responses.
_IDENTIFIER = {
    "accounts": "id",
    "transactions": "id",
    "plan": "id",
}

# Set of fields the agent may request via ``fields``; ``None`` (omitted) =>
# all fields. Strict so typos return 400 instead of dropping silently.
_ALLOWED_FIELDS = {
    "accounts": {
        "id",
        "nickname",
        "account_type",
        "current_balance",
        "original_balance",
        "interest_rate",
        "minimum_payment",
        "credit_limit",
        "due_day",
        "is_active",
        "is_debt",
        "payoff_progress",
        "created_at",
        "updated_at",
    },
    "transactions": {
        "id",
        "account_id",
        "account_nickname",
        "transaction_type",
        "amount",
        "description",
        "date",
        "created_at",
    },
    "plan": {
        "id",
        "strategy",
        "monthly_budget",
        "total_debt",
        "total_interest",
        "payoff_months",
        "payoff_date",
        "schedule_json",
        "is_active",
        "created_at",
        "updated_at",
    },
}

_ALLOWED_FILTERS = {
    "accounts": {"is_active", "account_type", "nickname", "is_debt"},
    "transactions": {
        "account_nickname",
        "account_id",
        "transaction_type",
        "min_amount",
        "max_amount",
    },
    "plan": {"is_active", "strategy"},
}

# Order-by must reference real DB columns. ``is_debt`` and ``payoff_progress``
# are Python @property methods on FinanceAccount (not columns), so they belong
# in ``_ALLOWED_FIELDS`` (serializable / requestable via ``fields=``) but NOT
# here — ordering by them would raise FieldError -> HTTP 500. Default: a
# resource's order-by allowlist mirrors its field allowlist; overrides below
# carve out non-column properties.
_ALLOWED_ORDER_BY = {
    "accounts": _ALLOWED_FIELDS["accounts"] - {"is_debt", "payoff_progress"},
    # ``account_nickname`` is serialized from ``account.nickname`` (a relation
    # traversal), not a column on FinanceTransaction — ordering by it would
    # raise FieldError -> HTTP 500. Drop it so order_by=account_nickname
    # returns a clean invalid_order_by 400.
    "transactions": _ALLOWED_FIELDS["transactions"] - {"account_nickname"},
    "plan": _ALLOWED_FIELDS["plan"],
}

# group_by must also reference real DB columns (qs.values(...)); ``is_debt`` is
# a property, not a column.
_ALLOWED_GROUP_BY = {
    "accounts": {"account_type"},
    "transactions": {"transaction_type", "account_id", "account_nickname", "date"},
    "plan": set(),
}

_DEFAULT_ORDER_BY = {
    "accounts": ("nickname",),
    "transactions": ("-date", "-created_at"),
    "plan": ("-created_at",),
}


def _parse_decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise QueryExecutionError("invalid_filter", f"{label} must be numeric") from exc


# ─── The view ──────────────────────────────────────────────────────────────


class FinanceQueryView(BaseQueryView):
    """Parameterized query for the Gravity ledger.

    Subclasses ``BaseQueryView`` — the base handles auth, RLS, window
    resolution, hashing, and the ``meta`` envelope. ``execute`` here picks
    the right resource handler and returns ``(data, row_count)``.
    """

    query_model: ClassVar[type[BaseModel]] = FinanceQueryRequest

    def execute(self, query, tenant: Tenant, window_resolved):
        # Validate cross-cutting params before dispatching.
        self._validate_filter_keys(query)
        self._validate_fields(query)
        self._validate_group_by(query)
        self._validate_aggregate(query)
        if query.resource == "transactions" and query.window is None:
            raise QueryExecutionError(
                "window_required",
                "resource='transactions' requires a window (use {'kind': 'all'} to opt out)",
            )

        if query.resource == "accounts":
            return self._execute_accounts(query, tenant)
        if query.resource == "transactions":
            return self._execute_transactions(query, tenant, window_resolved)
        if query.resource == "plan":
            return self._execute_plan(query, tenant)

        raise QueryExecutionError("unknown_resource", f"unknown resource: {query.resource!r}")

    # ── Accounts ───────────────────────────────────────────────────────

    def _execute_accounts(self, query: FinanceQueryRequest, tenant: Tenant):
        qs = FinanceAccount.objects.filter(tenant=tenant)
        f = query.filter
        if "is_active" in f:
            qs = qs.filter(is_active=bool(f["is_active"]))
        else:
            # Default: active only (matches agent expectation of "current state")
            qs = qs.filter(is_active=True)
        if "account_type" in f:
            qs = qs.filter(account_type=f["account_type"])
        if "is_debt" in f:
            qs = (
                qs.filter(account_type__in=FinanceAccount.DEBT_TYPES)
                if f["is_debt"]
                else qs.exclude(account_type__in=FinanceAccount.DEBT_TYPES)
            )
        if "nickname" in f:
            nick = str(f["nickname"]).strip()
            if nick:
                exact = qs.filter(nickname__iexact=nick)
                qs = exact if exact.exists() else qs.filter(nickname__icontains=nick)

        if query.aggregate is not None:
            return self._aggregate(qs, "accounts", query)

        qs = self._order(qs, query, "accounts")
        rows = list(qs[: query.limit])
        data = [self._serialize_account(a, query.fields) for a in rows]
        return data, len(data)

    # ── Transactions ───────────────────────────────────────────────────

    def _execute_transactions(self, query: FinanceQueryRequest, tenant: Tenant, window_resolved):
        qs = FinanceTransaction.objects.filter(tenant=tenant)
        if window_resolved is not None:
            qs = qs.filter(date__gte=window_resolved[0], date__lte=window_resolved[1])

        f = query.filter
        if "account_id" in f:
            qs = qs.filter(account_id=f["account_id"])
        if "account_nickname" in f:
            nick = str(f["account_nickname"]).strip()
            if nick:
                exact = FinanceAccount.objects.filter(tenant=tenant, nickname__iexact=nick)
                acct = exact.first() or FinanceAccount.objects.filter(tenant=tenant, nickname__icontains=nick).first()
                if acct is None:
                    return [], 0
                qs = qs.filter(account=acct)
        if "transaction_type" in f:
            qs = qs.filter(transaction_type=f["transaction_type"])
        if "min_amount" in f:
            qs = qs.filter(amount__gte=_parse_decimal(f["min_amount"], "min_amount"))
        if "max_amount" in f:
            qs = qs.filter(amount__lte=_parse_decimal(f["max_amount"], "max_amount"))

        if query.aggregate is not None:
            return self._aggregate(qs, "transactions", query)

        qs = qs.select_related("account")
        qs = self._order(qs, query, "transactions")
        rows = list(qs[: query.limit])
        data = [self._serialize_transaction(t, query.fields) for t in rows]
        return data, len(data)

    # ── Plan ───────────────────────────────────────────────────────────

    def _execute_plan(self, query: FinanceQueryRequest, tenant: Tenant):
        qs = PayoffPlan.objects.filter(tenant=tenant)
        f = query.filter
        if "is_active" in f:
            qs = qs.filter(is_active=bool(f["is_active"]))
        else:
            qs = qs.filter(is_active=True)
        if "strategy" in f:
            qs = qs.filter(strategy=f["strategy"])

        if query.aggregate is not None:
            return self._aggregate(qs, "plan", query)

        qs = self._order(qs, query, "plan")
        rows = list(qs[: query.limit])
        data = [self._serialize_plan(p, query.fields) for p in rows]
        return data, len(data)

    # ── Aggregation ────────────────────────────────────────────────────

    def _aggregate(self, qs: QuerySet, resource: str, query: FinanceQueryRequest):
        agg_cls = _AGG_MAP[query.aggregate]
        # count doesn't need a field; sum/avg/min/max do.
        if query.aggregate != "count" and not query.aggregate_field:
            raise QueryExecutionError(
                "aggregate_field_required",
                f"aggregate={query.aggregate} requires aggregate_field",
            )
        if query.aggregate == "count":
            agg_expr = agg_cls("id")
        else:
            # aggregate_field must be a numeric column on the model.
            if query.aggregate_field not in {
                "amount",
                "current_balance",
                "original_balance",
                "monthly_budget",
                "total_debt",
            }:
                raise QueryExecutionError(
                    "invalid_aggregate_field",
                    f"aggregate_field={query.aggregate_field!r} not allowed for resource={resource}",
                )
            agg_expr = agg_cls(query.aggregate_field)

        if query.group_by:
            group_field = query.group_by
            # account_nickname requires a join through the account relation.
            if group_field == "account_nickname" and resource == "transactions":
                qs = qs.values("account__nickname").annotate(value=agg_expr, count=Count("id"))
                rows = list(qs)
                data = [
                    {"group": {"account_nickname": r["account__nickname"]}, "value": r["value"], "count": r["count"]}
                    for r in rows
                ]
            else:
                qs = qs.values(group_field).annotate(value=agg_expr, count=Count("id"))
                rows = list(qs)
                data = [
                    {"group": {group_field: r[group_field]}, "value": r["value"], "count": r["count"]} for r in rows
                ]
            data = jsonify(data)
            return data, len(data)

        # No group_by — single-row response
        result = qs.aggregate(value=agg_expr, count=Count("id"))
        data = [{"value": jsonify(result["value"]) if result["value"] is not None else None, "count": result["count"]}]
        return data, 1

    # ── Ordering helper ────────────────────────────────────────────────

    def _order(self, qs: QuerySet, query: FinanceQueryRequest, resource: str) -> QuerySet:
        if query.order_by:
            field = query.order_by.lstrip("-")
            # Order-by must hit a real DB column; fall back to the field
            # allowlist for resources whose serializable fields are all columns.
            allowed = _ALLOWED_ORDER_BY.get(resource, _ALLOWED_FIELDS[resource])
            if field not in allowed:
                raise QueryExecutionError(
                    "invalid_order_by",
                    f"order_by={query.order_by!r} not allowed for resource={resource}",
                )
            return qs.order_by(query.order_by)
        return qs.order_by(*_DEFAULT_ORDER_BY[resource])

    # ── Validation helpers ────────────────────────────────────────────

    @staticmethod
    def _validate_filter_keys(query: FinanceQueryRequest) -> None:
        allowed = _ALLOWED_FILTERS[query.resource]
        unknown = set(query.filter.keys()) - allowed
        if unknown:
            raise QueryExecutionError(
                "unknown_filter_keys",
                f"filter keys {sorted(unknown)!r} not allowed for resource={query.resource}; "
                f"allowed: {sorted(allowed)!r}",
            )

    @staticmethod
    def _validate_fields(query: FinanceQueryRequest) -> None:
        if not query.fields:
            return
        allowed = _ALLOWED_FIELDS[query.resource]
        unknown = set(query.fields) - allowed
        if unknown:
            raise QueryExecutionError(
                "unknown_fields",
                f"fields {sorted(unknown)!r} not allowed for resource={query.resource}; allowed: {sorted(allowed)!r}",
            )

    @staticmethod
    def _validate_group_by(query: FinanceQueryRequest) -> None:
        if not query.group_by:
            return
        allowed = _ALLOWED_GROUP_BY[query.resource]
        if query.group_by not in allowed:
            raise QueryExecutionError(
                "unknown_group_by",
                f"group_by={query.group_by!r} not allowed for resource={query.resource}; allowed: {sorted(allowed)!r}",
            )
        if query.aggregate is None:
            raise QueryExecutionError(
                "group_by_requires_aggregate",
                "group_by requires aggregate to be set",
            )

    @staticmethod
    def _validate_aggregate(query: FinanceQueryRequest) -> None:
        if query.aggregate is None and query.aggregate_field:
            raise QueryExecutionError(
                "aggregate_field_without_aggregate",
                "aggregate_field is set but aggregate is None",
            )

    # ── Row serializers (hint-based fields) ────────────────────────────

    @staticmethod
    def _serialize_account(a: FinanceAccount, fields: list[str] | None) -> dict[str, Any]:
        full = {
            "id": str(a.id),
            "nickname": a.nickname,
            "account_type": a.account_type,
            "current_balance": str(a.current_balance),
            "original_balance": str(a.original_balance) if a.original_balance is not None else None,
            "interest_rate": str(a.interest_rate) if a.interest_rate is not None else None,
            "minimum_payment": str(a.minimum_payment) if a.minimum_payment is not None else None,
            "credit_limit": str(a.credit_limit) if a.credit_limit is not None else None,
            "due_day": a.due_day,
            "is_active": a.is_active,
            "is_debt": a.is_debt,
            "payoff_progress": a.payoff_progress,
            "created_at": a.created_at.isoformat(),
            "updated_at": a.updated_at.isoformat(),
        }
        return _project(full, fields, identifier="id")

    @staticmethod
    def _serialize_transaction(t: FinanceTransaction, fields: list[str] | None) -> dict[str, Any]:
        full = {
            "id": str(t.id),
            "account_id": str(t.account_id),
            "account_nickname": t.account.nickname,
            "transaction_type": t.transaction_type,
            "amount": str(t.amount),
            "description": t.description,
            "date": t.date.isoformat(),
            "created_at": t.created_at.isoformat(),
        }
        return _project(full, fields, identifier="id")

    @staticmethod
    def _serialize_plan(p: PayoffPlan, fields: list[str] | None) -> dict[str, Any]:
        full = {
            "id": str(p.id),
            "strategy": p.strategy,
            "monthly_budget": str(p.monthly_budget),
            "total_debt": str(p.total_debt),
            "total_interest": str(p.total_interest),
            "payoff_months": p.payoff_months,
            "payoff_date": p.payoff_date.isoformat(),
            "schedule_json": p.schedule_json,
            "is_active": p.is_active,
            "created_at": p.created_at.isoformat(),
            "updated_at": p.updated_at.isoformat(),
        }
        return _project(full, fields, identifier="id")


def _project(full: dict[str, Any], fields: list[str] | None, *, identifier: str) -> dict[str, Any]:
    """Hint-based field projection: always include identifier; include all if
    ``fields`` is None; otherwise include identifier + listed fields."""
    if fields is None:
        return full
    keep = set(fields) | {identifier}
    return {k: v for k, v in full.items() if k in keep}
