"""Usage aggregation and transparency services."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.tenants.models import Tenant
from .constants import (
    DEFAULT_RATE,
    MODEL_RATES,
    PLATFORM_MARGIN_TARGET,
)
from .models import MonthlyBudget, UsageRecord


def get_month_boundaries(ref: date | None = None) -> tuple[date, date]:
    """Return (first_day, last_day) of the month containing ref."""
    if ref is None:
        ref = timezone.now().date()
    first = ref.replace(day=1)
    if first.month == 12:
        last = first.replace(year=first.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last = first.replace(month=first.month + 1, day=1) - timedelta(days=1)
    return first, last


def get_usage_summary(tenant: Tenant, ref_date: date | None = None) -> dict:
    """Current month summary: totals + per-model breakdown."""
    first, last = get_month_boundaries(ref_date)
    qs = UsageRecord.objects.filter(
        tenant=tenant,
        created_at__date__gte=first,
        created_at__date__lte=last,
    )

    totals = qs.aggregate(
        total_input=Sum("input_tokens", default=0),
        total_output=Sum("output_tokens", default=0),
        total_cost=Sum("cost_estimate", default=Decimal("0")),
        message_count=Count("id"),
    )

    by_model = (
        qs.values("model_used")
        .annotate(
            input_tokens=Sum("input_tokens", default=0),
            output_tokens=Sum("output_tokens", default=0),
            cost=Sum("cost_estimate", default=Decimal("0")),
            count=Count("id"),
        )
        .order_by("-cost")
    )

    # Budget info
    budget_info = _get_budget_info(tenant, first)

    return {
        "period": {"start": first.isoformat(), "end": last.isoformat()},
        "total_input_tokens": totals["total_input"],
        "total_output_tokens": totals["total_output"],
        "total_tokens": totals["total_input"] + totals["total_output"],
        "total_cost": float(totals["total_cost"]),
        "message_count": totals["message_count"],
        "by_model": [
            {
                "model": row["model_used"],
                "display_name": MODEL_RATES.get(row["model_used"], DEFAULT_RATE)["display_name"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cost": float(row["cost"]),
                "count": row["count"],
            }
            for row in by_model
        ],
        "budget": budget_info,
    }


def _get_budget_info(tenant: Tenant, first_of_month: date) -> dict:
    """Budget remaining for tenant and global."""
    tenant.refresh_from_db()
    try:
        budget = MonthlyBudget.objects.get(month=first_of_month)
        global_remaining = budget.remaining
        global_spent = float(budget.spent_dollars)
    except MonthlyBudget.DoesNotExist:
        global_remaining = None
        global_spent = 0.0

    return {
        "tenant_tokens_used": tenant.tokens_this_month,
        "tenant_token_budget": tenant.monthly_token_budget,
        "tenant_estimated_cost": float(tenant.estimated_cost_this_month),
        "budget_percentage": (
            round(tenant.tokens_this_month / tenant.monthly_token_budget * 100, 1)
            if tenant.monthly_token_budget > 0 else 0
        ),
        "global_spent": global_spent,
        "global_remaining": float(global_remaining) if global_remaining is not None else None,
    }


def get_daily_usage(tenant: Tenant, days: int = 30) -> list[dict]:
    """Daily aggregation for the last N days."""
    start = timezone.now().date() - timedelta(days=days - 1)
    qs = (
        UsageRecord.objects.filter(tenant=tenant, created_at__date__gte=start)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            input_tokens=Sum("input_tokens", default=0),
            output_tokens=Sum("output_tokens", default=0),
            cost=Sum("cost_estimate", default=Decimal("0")),
            count=Count("id"),
        )
        .order_by("day")
    )

    # Fill in missing days with zeros
    results_by_day = {row["day"]: row for row in qs}
    daily = []
    current = start
    today = timezone.now().date()
    while current <= today:
        if current in results_by_day:
            r = results_by_day[current]
            daily.append({
                "date": current.isoformat(),
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cost": float(r["cost"]),
                "message_count": r["count"],
            })
        else:
            daily.append({
                "date": current.isoformat(),
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": 0.0,
                "message_count": 0,
            })
        current += timedelta(days=1)

    return daily


def get_transparency_data(tenant: Tenant) -> dict:
    """Open-books transparency: cost breakdown, margin, model rates."""
    first, last = get_month_boundaries()
    qs = UsageRecord.objects.filter(
        tenant=tenant,
        created_at__date__gte=first,
        created_at__date__lte=last,
    )

    totals = qs.aggregate(
        total_cost=Sum("cost_estimate", default=Decimal("0")),
        message_count=Count("id"),
    )

    actual_cost = float(totals["total_cost"])
    subscription = _get_subscription_price()
    # Build rate card
    rate_card = []
    seen = set()
    for key, rate in MODEL_RATES.items():
        name = rate["display_name"]
        if name in seen:
            continue
        seen.add(name)
        rate_card.append({
            "model": key,
            "display_name": name,
            "input_per_million": rate["input"],
            "output_per_million": rate["output"],
        })

    infra_breakdown = {
        "container": 4.00,
        "database_share": 0.50,
        "storage_share": 0.25,
        "total": 4.75,
    }
    platform_margin = max(subscription - actual_cost, 0.0)
    platform_other = max(platform_margin - infra_breakdown["total"], 0)
    platform_total = round(platform_margin, 4)
    margin_pct = (platform_total / subscription * 100) if subscription > 0 else 0.0

    return {
        "period": {"start": first.isoformat(), "end": last.isoformat()},
        "subscription_price": subscription,
        "your_actual_cost": round(actual_cost, 4),
        "platform_margin": platform_total,
        "margin_percentage": round(margin_pct, 1),
        "target_margin_percentage": PLATFORM_MARGIN_TARGET * 100,
        "message_count": totals["message_count"],
        "model_rates": rate_card,
        "infra_breakdown": infra_breakdown,
        "explanation": (
            f"You pay ${subscription:.2f}/mo. This month your AI usage actually cost "
            f"${actual_cost:.4f}. Platform & Infrastructure costs were estimated at "
            f"${infra_breakdown['total']:.2f} (container ${infra_breakdown['container']:.2f}, "
            f"database ${infra_breakdown['database_share']:.2f}, storage ${infra_breakdown['storage_share']:.2f}) "
            f"with an additional ${platform_other:.4f} for ops/development."
        ),
    }


def _get_subscription_price() -> float:
    default = 12.0
    configured = getattr(settings, "USAGE_DASHBOARD_SUBSCRIPTION_PRICE", default)
    try:
        value = float(configured)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default
