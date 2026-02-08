"""Billing services — Stripe webhook handling and usage tracking."""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db.models import F

from apps.tenants.models import Tenant
from .models import MonthlyBudget, UsageRecord

logger = logging.getLogger(__name__)

# Cost per 1M tokens (approximate, for budget tracking)
MODEL_COSTS: dict[str, dict[str, float]] = {
    "anthropic/claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "anthropic/claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
}
DEFAULT_COST = {"input": 3.0, "output": 15.0}


def record_usage(
    tenant: Tenant,
    event_type: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model_used: str = "",
) -> UsageRecord:
    """Record a usage event and update tenant counters."""
    costs = MODEL_COSTS.get(model_used, DEFAULT_COST)
    cost = Decimal(str(
        (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000
    ))

    record = UsageRecord.objects.create(
        tenant=tenant,
        event_type=event_type,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_used=model_used,
        cost_estimate=cost,
    )

    total_tokens = input_tokens + output_tokens
    Tenant.objects.filter(id=tenant.id).update(
        messages_today=F("messages_today") + (1 if event_type == "message" else 0),
        messages_this_month=F("messages_this_month") + (1 if event_type == "message" else 0),
        tokens_this_month=F("tokens_this_month") + total_tokens,
        estimated_cost_this_month=F("estimated_cost_this_month") + cost,
    )

    # Update global budget
    today = date.today()
    first_of_month = today.replace(day=1)
    budget, _ = MonthlyBudget.objects.get_or_create(
        month=first_of_month,
        defaults={"budget_dollars": 100},
    )
    MonthlyBudget.objects.filter(id=budget.id).update(
        spent_dollars=F("spent_dollars") + cost,
    )

    return record


def check_budget(tenant: Tenant) -> bool:
    """Return True if tenant can send messages (within budget)."""
    tenant.refresh_from_db()
    if tenant.is_over_budget:
        logger.warning("Tenant %s over personal budget", tenant.id)
        return False

    today = date.today()
    first_of_month = today.replace(day=1)
    try:
        budget = MonthlyBudget.objects.get(month=first_of_month)
        if budget.is_over_budget:
            logger.warning("Global monthly budget exceeded")
            return False
    except MonthlyBudget.DoesNotExist:
        pass

    return True


def handle_checkout_completed(session_data: dict) -> None:
    """Handle Stripe checkout.session.completed webhook."""
    from apps.orchestrator.tasks import provision_tenant_task

    metadata = session_data.get("metadata", {})
    user_id = metadata.get("user_id")
    tier = metadata.get("tier", "basic")
    customer_id = session_data.get("customer", "")
    subscription_id = session_data.get("subscription", "")

    if not user_id:
        logger.error("checkout.session.completed missing user_id in metadata")
        return

    try:
        tenant = Tenant.objects.get(user_id=user_id)
    except Tenant.DoesNotExist:
        logger.error("No tenant for user_id=%s", user_id)
        return

    tenant.stripe_customer_id = customer_id
    tenant.stripe_subscription_id = subscription_id
    tenant.model_tier = tier
    tenant.status = Tenant.Status.PROVISIONING
    tenant.save(update_fields=[
        "stripe_customer_id", "stripe_subscription_id",
        "model_tier", "status", "updated_at",
    ])

    provision_tenant_task.delay(str(tenant.id))
    logger.info("Triggered provisioning for tenant %s", tenant.id)


def handle_subscription_deleted(subscription_data: dict) -> None:
    """Handle customer.subscription.deleted webhook."""
    from apps.orchestrator.tasks import deprovision_tenant_task

    metadata = subscription_data.get("metadata", {})
    user_id = metadata.get("user_id")

    if not user_id:
        logger.error("subscription.deleted missing user_id in metadata")
        return

    try:
        tenant = Tenant.objects.get(user_id=user_id)
    except Tenant.DoesNotExist:
        logger.error("No tenant for user_id=%s", user_id)
        return

    tenant.status = Tenant.Status.DEPROVISIONING
    tenant.save(update_fields=["status", "updated_at"])

    deprovision_tenant_task.delay(str(tenant.id))
    logger.info("Triggered deprovisioning for tenant %s", tenant.id)
