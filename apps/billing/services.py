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


def _normalize_tier(raw_tier: str) -> str:
    allowed = {choice for choice, _ in Tenant.ModelTier.choices}
    if raw_tier in allowed:
        return raw_tier
    logger.warning("Invalid tier '%s' from Stripe webhook, defaulting to starter", raw_tier)
    return Tenant.ModelTier.STARTER


def _find_tenant_for_stripe_event(payload: dict) -> Tenant | None:
    metadata = payload.get("metadata") or {}
    user_id = metadata.get("user_id")
    subscription_id = payload.get("subscription") or payload.get("id")
    customer_id = payload.get("customer")

    if user_id:
        tenant = Tenant.objects.filter(user_id=user_id).first()
        if tenant:
            return tenant

    if subscription_id:
        tenant = Tenant.objects.filter(stripe_subscription_id=subscription_id).first()
        if tenant:
            return tenant

    if customer_id:
        tenant = Tenant.objects.filter(stripe_customer_id=customer_id).first()
        if tenant:
            return tenant

    return None


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
    # Trial users share starter-tier budget behavior.
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
    from apps.cron.publish import publish_task

    metadata = session_data.get("metadata") or {}
    tier = _normalize_tier(metadata.get("tier", Tenant.ModelTier.STARTER))
    customer_id = session_data.get("customer") or ""
    subscription_id = session_data.get("subscription") or ""

    tenant = _find_tenant_for_stripe_event(session_data)
    if not tenant:
        logger.error("No tenant found for checkout.session.completed payload")
        return

    was_provisioning = tenant.status == Tenant.Status.PROVISIONING
    same_subscription = tenant.stripe_subscription_id == subscription_id
    already_active = tenant.status == Tenant.Status.ACTIVE and bool(tenant.container_id)

    if already_active and same_subscription and tenant.model_tier == tier:
        logger.info("Ignoring duplicate checkout completion for active tenant %s", tenant.id)
        return

    tenant.stripe_customer_id = customer_id
    tenant.stripe_subscription_id = subscription_id
    tenant.model_tier = tier
    tenant.is_trial = False

    if tenant.status == Tenant.Status.SUSPENDED:
        tenant.status = Tenant.Status.ACTIVE
        should_provision = False
    else:
        tenant.status = Tenant.Status.PROVISIONING
        should_provision = True

    tenant.save(update_fields=[
        "stripe_customer_id", "stripe_subscription_id",
        "model_tier", "is_trial", "status", "updated_at",
    ])

    if was_provisioning and same_subscription:
        logger.info("Tenant %s already provisioning for current subscription", tenant.id)
        return

    if should_provision:
        publish_task("provision_tenant", str(tenant.id))
        logger.info("Triggered provisioning for tenant %s", tenant.id)


def handle_subscription_deleted(subscription_data: dict) -> None:
    """Handle customer.subscription.deleted webhook."""
    from apps.cron.publish import publish_task

    tenant = _find_tenant_for_stripe_event(subscription_data)
    if not tenant:
        logger.error("No tenant found for customer.subscription.deleted payload")
        return

    if tenant.status in (Tenant.Status.DEPROVISIONING, Tenant.Status.DELETED):
        logger.info("Tenant %s already deprovisioning/deleted", tenant.id)
        return

    tenant.status = Tenant.Status.DEPROVISIONING
    tenant.save(update_fields=["status", "updated_at"])

    publish_task("deprovision_tenant", str(tenant.id))
    logger.info("Triggered deprovisioning for tenant %s", tenant.id)


def handle_invoice_payment_failed(invoice_data: dict) -> None:
    """Handle invoice.payment_failed webhook by suspending tenant access."""
    tenant = _find_tenant_for_stripe_event(invoice_data)
    if not tenant:
        logger.error("No tenant found for invoice.payment_failed payload")
        return

    if tenant.status == Tenant.Status.SUSPENDED:
        logger.info("Tenant %s already suspended", tenant.id)
        return

    tenant.status = Tenant.Status.SUSPENDED
    tenant.save(update_fields=["status", "updated_at"])
    logger.warning("Suspended tenant %s after failed invoice", tenant.id)
