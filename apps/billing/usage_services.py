"""Usage aggregation and transparency services."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.tenants.models import Tenant

from .constants import (
    MODEL_RATES,
    display_name_for_model,
)
from .models import MonthlyBudget, UsageRecord

logger = logging.getLogger(__name__)

# Cache TTL for a resolved Stripe subscription price. Prices change rarely and
# the transparency dashboard renders on every page view, so an hour of staleness
# is an acceptable trade for not calling Stripe per request.
_SUBSCRIPTION_PRICE_CACHE_TTL = 3600

# Short TTL for negative-caching a *failed* Stripe lookup. Without this, a Stripe
# outage would make EVERY dashboard render fire a failing API call — and stripe-py's
# default HTTP timeout is long (tens of seconds) — so the usage page would hang
# per-render for the outage's duration. Capping at one slow call per subscription
# per 5 minutes bounds that blast radius while still recovering promptly.
_SUBSCRIPTION_PRICE_FAILURE_CACHE_TTL = 300


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
                "display_name": display_name_for_model(row["model_used"] or ""),
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

    effective_token_budget = tenant.effective_token_budget
    effective_cost = tenant.effective_cost_budget
    cost_used = float(tenant.estimated_cost_this_month)
    cost_budget = float(effective_cost)
    return {
        # Token fields kept for informational display
        "tenant_tokens_used": tenant.tokens_this_month,
        "tenant_token_budget": effective_token_budget,
        "tenant_estimated_cost": cost_used,
        # Cost-based budget (drives enforcement)
        "tenant_cost_used": cost_used,
        "tenant_cost_budget": cost_budget,
        "budget_percentage": (round(cost_used / cost_budget * 100, 1) if cost_budget > 0 else 0),
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
            daily.append(
                {
                    "date": current.isoformat(),
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cost": float(r["cost"]),
                    "message_count": r["count"],
                }
            )
        else:
            daily.append(
                {
                    "date": current.isoformat(),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost": 0.0,
                    "message_count": 0,
                }
            )
        current += timedelta(days=1)

    return daily


def _get_infra_breakdown(tenant: Tenant, month: date) -> dict:
    """Load real infra costs from InfraCostSnapshot, or fall back to estimates."""
    from .models import InfraCostSnapshot

    try:
        snapshot = InfraCostSnapshot.objects.get(tenant=tenant, month=month)
        return {
            "container": float(snapshot.container_cost),
            "database_share": float(snapshot.database_share),
            "storage_share": float(snapshot.storage_cost),
            "total": float(snapshot.total_cost),
            "source": snapshot.source,
        }
    except InfraCostSnapshot.DoesNotExist:
        # No snapshot yet (brand-new tenant / before the first cron run). Mirror
        # the cron's estimate exactly — same container/storage estimates and the
        # same capped database share — so the transparency figure doesn't jump
        # when the first snapshot lands.
        from .infra_cost_service import ESTIMATE_CONTAINER, ESTIMATE_STORAGE, calculate_database_share

        active_count = (
            Tenant.objects.filter(status="active", container_id__isnull=False).exclude(container_id="").count()
        )
        container = float(ESTIMATE_CONTAINER)
        storage = float(ESTIMATE_STORAGE)
        db_share = float(calculate_database_share(active_count))
        return {
            "container": container,
            "database_share": db_share,
            "storage_share": storage,
            "total": round(container + storage + db_share, 4),
            "source": "estimate",
        }


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

    from apps.orchestrator.config_generator import TIER_MODEL_CONFIGS

    # Build rate card filtered to tenant's tier
    tier_model_keys = set(TIER_MODEL_CONFIGS.get(tenant.model_tier, {}).keys())
    rate_card = []
    seen = set()
    for key, rate in MODEL_RATES.items():
        name = rate["display_name"]
        if name in seen:
            continue
        # For non-BYOK tiers, only show tier-relevant models
        if tier_model_keys and key not in tier_model_keys:
            continue
        seen.add(name)
        rate_card.append(
            {
                "model": key,
                "display_name": name,
                "input_per_million": rate["input"],
                "output_per_million": rate["output"],
            }
        )
    # BYOK or empty tier: show all models
    if not rate_card:
        seen_fallback = set()
        for key, rate in MODEL_RATES.items():
            name = rate["display_name"]
            if name in seen_fallback:
                continue
            seen_fallback.add(name)
            rate_card.append(
                {
                    "model": key,
                    "display_name": name,
                    "input_per_million": rate["input"],
                    "output_per_million": rate["output"],
                }
            )

    infra_breakdown = _get_infra_breakdown(tenant, first)

    subscription_price = _get_subscription_price(tenant)
    surplus = max(0.0, subscription_price - actual_cost - infra_breakdown["total"])
    surplus = round(surplus, 4)

    donation_amount = 0.0
    if tenant.donation_enabled and surplus > 0:
        donation_amount = round(surplus * (tenant.donation_percentage / 100), 4)

    return {
        "period": {"start": first.isoformat(), "end": last.isoformat()},
        "subscription_price": subscription_price,
        "your_actual_cost": round(actual_cost, 4),
        "platform_infra": infra_breakdown["total"],
        "surplus": surplus,
        "donation_amount": donation_amount,
        "donation_enabled": tenant.donation_enabled,
        "donation_percentage": tenant.donation_percentage,
        "message_count": totals["message_count"],
        "model_rates": rate_card,
        "infra_breakdown": infra_breakdown,
        "explanation": (
            f"This month your AI usage cost ${actual_cost:.4f}. "
            f"Platform infrastructure is estimated at "
            f"${infra_breakdown['total']:.2f}/mo (container ${infra_breakdown['container']:.2f}, "
            f"database ${infra_breakdown['database_share']:.2f}, storage ${infra_breakdown['storage_share']:.2f})."
        ),
    }


def _subscription_price_fallback() -> float:
    """Configured monthly price to use when the live Stripe price is unavailable."""
    from django.conf import settings

    return float(getattr(settings, "USAGE_DASHBOARD_SUBSCRIPTION_PRICE", 12.0))


def _normalize_to_monthly(amount: float, interval: str | None, interval_count: int) -> float | None:
    """Normalize a recurring price to a monthly USD figure.

    ``amount`` is the per-billing-period price (already in dollars). We only
    normalize the two intervals this product actually bills on — monthly and
    yearly (yearly / 12). Anything else (week/day, or an unrecognized interval)
    returns ``None`` so the caller falls back rather than guess.
    """
    if not interval_count or interval_count < 1:
        interval_count = 1
    if interval == "month":
        return amount / interval_count
    if interval == "year":
        return amount / (12 * interval_count)
    return None


def _fetch_subscription_price_from_stripe(subscription_id: str) -> float | None:
    """Return the normalized monthly USD price for a Stripe subscription.

    Returns ``None`` on ANY problem — missing API key, Stripe error/timeout, no
    line item, non-USD currency, or a non-monthly/yearly interval — so the caller
    can fall back. Never raises.

    We hit the Stripe API directly (expanding the price) rather than djstripe's
    local models: djstripe is installed but its tables are NOT webhook-synced in
    this codebase (the custom handler in ``billing/views.py`` writes ``Tenant``
    fields, not djstripe records), so those models can't be trusted to be
    populated. ``Subscription.to_dict()`` coerces the StripeObject (and its
    nested expansions) to a plain dict — the same boundary
    ``billing/views.py`` uses, robust across stripe-py 14.x/15.x.
    """
    import stripe

    # Reuse the webhook module's mode→key resolver so "which key for live vs test"
    # lives in exactly one place. Function-local import — views.py doesn't import
    # this module, so there's no cycle, but keep it local to be safe.
    from .views import _get_stripe_api_key

    try:
        api_key = (_get_stripe_api_key() or "").strip()
        if not api_key:
            logger.info("billing: Stripe API key missing — cannot resolve live subscription price")
            return None

        subscription = stripe.Subscription.retrieve(
            subscription_id,
            expand=["items.data.price"],
            api_key=api_key,
        )
        data = subscription.to_dict() if hasattr(subscription, "to_dict") else subscription

        items = ((data.get("items") or {}).get("data")) or []
        if not items:
            logger.info("billing: subscription %s has no line items — fallback", subscription_id)
            return None

        price = items[0].get("price") or {}
        unit_amount = price.get("unit_amount")
        currency = (price.get("currency") or "").lower()
        recurring = price.get("recurring") or {}
        interval = recurring.get("interval")
        interval_count = recurring.get("interval_count") or 1

        if unit_amount is None:
            logger.info("billing: subscription %s price has no unit_amount — fallback", subscription_id)
            return None
        if currency and currency != "usd":
            logger.info("billing: subscription %s priced in %s (not USD) — fallback", subscription_id, currency)
            return None

        monthly = _normalize_to_monthly(float(unit_amount) / 100.0, interval, interval_count)
        if monthly is None:
            logger.info(
                "billing: subscription %s has unsupported interval %r×%s — fallback",
                subscription_id,
                interval,
                interval_count,
            )
            return None
        return round(monthly, 4)
    except Exception:
        # Stripe API error, timeout, network blip, malformed payload — anything.
        # This runs inside a user-facing dashboard render, so we swallow and fall
        # back rather than 500 the page.
        logger.info(
            "billing: failed to resolve subscription price from Stripe (sub=%s) — fallback",
            subscription_id,
            exc_info=True,
        )
        return None


def _get_subscription_price(tenant: Tenant) -> float:
    """Resolve the tenant's monthly subscription price in USD.

    Drives the surplus/donation figures on the transparency dashboard — and an
    in-flight donation ledger will use it to compute real disbursement amounts —
    so a wrong number becomes wrong money. Reads the live price from Stripe
    (cached ~1h per subscription) instead of trusting a hardcoded figure, but
    NEVER raises: any failure falls back to ``USAGE_DASHBOARD_SUBSCRIPTION_PRICE``.

    Tenants with no ``stripe_subscription_id`` (trials, comped accounts) fall
    back to the setting, preserving the previous flat-$12 behavior.
    """
    from django.core.cache import cache

    fallback = _subscription_price_fallback()

    subscription_id = (tenant.stripe_subscription_id or "").strip()
    if not subscription_id:
        # No subscription to price against (trial/comped) — expected, not drift.
        return fallback

    cache_key = f"billing:sub_price:{subscription_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return float(cached)

    # Negative cache: if a recent lookup for this subscription failed, serve the
    # fallback without touching Stripe. We store a marker (not the price) so a
    # setting change during an outage is still honored via the fresh `fallback`.
    failure_key = f"billing:sub_price_fallback:{subscription_id}"
    if cache.get(failure_key) is not None:
        return fallback

    price = _fetch_subscription_price_from_stripe(subscription_id)
    if price is None:
        cache.set(failure_key, True, _SUBSCRIPTION_PRICE_FAILURE_CACHE_TTL)
        logger.info(
            "billing: subscription price fallback for tenant %s (sub=%s) → $%.2f",
            tenant.id,
            subscription_id,
            fallback,
        )
        return fallback

    cache.set(cache_key, price, _SUBSCRIPTION_PRICE_CACHE_TTL)
    return price
