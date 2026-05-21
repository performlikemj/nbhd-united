"""Billing services — Stripe webhook handling and usage tracking."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db.models import F

from apps.tenants.models import Tenant

from .constants import DEFAULT_RATE as MODELS_DEFAULT_RATE
from .constants import MODEL_RATES
from .models import MonthlyBudget, UsageRecord

logger = logging.getLogger(__name__)

# Cost per 1M tokens (approximate, for budget tracking)
MODEL_COSTS: dict[str, dict[str, float]] = {
    key: {"input": value["input"], "output": value["output"]} for key, value in MODEL_RATES.items()
}
DEFAULT_COST = {"input": MODELS_DEFAULT_RATE["input"], "output": MODELS_DEFAULT_RATE["output"]}


def _normalize_tier(raw_tier: str) -> str:
    if raw_tier != Tenant.ModelTier.STARTER:
        logger.warning("Non-starter tier '%s' from Stripe webhook, defaulting to starter", raw_tier)
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


# The chat-completions gateway request always sends ``"model": "openclaw"``
# because the actual model is chosen inside the OpenClaw runtime, not by the
# Django caller. The response echoes that placeholder back in the OpenAI-spec
# top-level ``model`` field, while the real upstream model id (e.g.
# ``openrouter/minimax/minimax-m2.7`` or ``anthropic/claude-sonnet-4.6``) is
# reported in OpenClaw's custom ``usage.model_used`` field. Anything that
# records usage from a gateway response must prefer the usage-level fields,
# or every record will be tagged as the "openclaw" placeholder and the
# per-model breakdown collapses into one undifferentiated bucket.
_OPENCLAW_PLACEHOLDER = "openclaw"


def extract_model_from_response(result: object) -> str:
    """Resolve the actual upstream model id from an OpenClaw chat response.

    Tries (in order) ``usage.model_used`` → ``usage.model`` →
    top-level ``model_used`` → top-level ``model``. The ``"openclaw"``
    request-side placeholder is treated as no-signal and skipped.
    Returns ``""`` if nothing usable is found.
    """
    if not isinstance(result, dict):
        return ""

    candidates: list[str] = []
    usage = result.get("usage")
    if isinstance(usage, dict):
        for key in ("model_used", "model"):
            value = usage.get(key)
            if isinstance(value, str):
                candidates.append(value)
    for key in ("model_used", "model"):
        value = result.get(key)
        if isinstance(value, str):
            candidates.append(value)

    for value in candidates:
        stripped = value.strip()
        if stripped and stripped != _OPENCLAW_PLACEHOLDER:
            return stripped
    return ""


def record_usage(
    tenant: Tenant,
    event_type: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model_used: str = "",
    *,
    is_system: bool = False,
) -> UsageRecord:
    """Record a usage event.

    Tenant counters (``estimated_cost_this_month``, ``tokens_this_month``,
    message counts) are updated only for user-attributed events. When
    ``is_system=True`` the row is written and ``MonthlyBudget.spent_dollars``
    is still incremented (the platform still pays for it), but the per-tenant
    counters that drive quota enforcement are left alone. Phase 4's weekly
    reflection synthesis uses this path.
    """
    costs = MODEL_COSTS.get(model_used, DEFAULT_COST)
    cost = Decimal(str((input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000))

    record = UsageRecord.objects.create(
        tenant=tenant,
        event_type=event_type,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_used=model_used,
        cost_estimate=cost,
        is_system_event=is_system,
    )

    if not is_system:
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


def check_budget(tenant: Tenant) -> str:
    """Return '' if within budget, or the block reason ('personal'/'global').

    Checks personal cost budget first, then global platform budget.
    Callers should hibernate the container when a reason is returned.
    """
    # Personal budget
    tenant.refresh_from_db(
        fields=["estimated_cost_this_month", "monthly_cost_budget", "model_tier", "is_budget_exempt"]
    )
    if tenant.is_budget_exempt:
        return ""
    if tenant.is_over_budget:
        return "personal"

    # Global platform budget
    from .models import MonthlyBudget

    today = date.today()
    first_of_month = today.replace(day=1)
    try:
        global_budget = MonthlyBudget.objects.get(month=first_of_month)
        if global_budget.remaining is not None and global_budget.remaining <= 0:
            return "global"
    except MonthlyBudget.DoesNotExist:
        pass

    return ""


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
    # Reset to tier default when plan changes (0 = use tier default)
    tenant.monthly_token_budget = 0
    tenant.monthly_cost_budget = 0

    if tenant.status == Tenant.Status.SUSPENDED:
        tenant.status = Tenant.Status.ACTIVE
        tenant.hibernated_at = None  # Clear idle hibernation if set
        should_provision = False
        should_wake = True  # Wake hibernated container
    else:
        tenant.status = Tenant.Status.PROVISIONING
        should_provision = True
        should_wake = False

    tenant.save(
        update_fields=[
            "stripe_customer_id",
            "stripe_subscription_id",
            "model_tier",
            "is_trial",
            "status",
            "monthly_token_budget",
            "monthly_cost_budget",
            "hibernated_at",
            "updated_at",
        ]
    )

    if was_provisioning and same_subscription:
        logger.info("Tenant %s already provisioning for current subscription", tenant.id)
        return

    if should_wake and tenant.container_id:
        try:
            from apps.orchestrator.azure_client import scale_container_app

            scale_container_app(tenant.container_id, min_replicas=1, max_replicas=1)
            logger.info("Woke container %s for reactivated tenant %s", tenant.container_id, tenant.id)

            # Apply pending config updates missed during hibernation
            try:
                tenant.refresh_from_db(fields=["config_version", "pending_config_version"])
                if tenant.pending_config_version > tenant.config_version:
                    publish_task("apply_single_tenant_config", str(tenant.id))
                    logger.info("Queued config apply for reactivated tenant %s", tenant.id)
            except Exception:
                logger.exception("Failed to queue config apply for tenant %s", tenant.id)

            # Re-enable cron jobs that were disabled during suspension. The
            # container just started waking and its gateway is not listening
            # yet (cold-start typically 30-60s), so we cannot call
            # ``resume_tenant_crons`` synchronously here — every reactivation
            # would 502 inside the webhook handler and the crons would stay
            # disabled until the hourly reconcile sweep caught the
            # ``enabled``-field drift. See issue #540. Delay the resume via
            # QStash so it fires after the gateway is ready, and let QStash
            # retries cover any residual cold-start.
            try:
                publish_task(
                    "resume_tenant_crons",
                    str(tenant.id),
                    idempotency_key=f"resume-crons-{tenant.id}",
                    delay_seconds=30,
                )
                logger.info(
                    "Queued resume_tenant_crons for reactivated tenant %s (delay=30s)",
                    tenant.id,
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue resume_tenant_crons for tenant %s — "
                    "hourly reconcile_tenant_crons sweep is the safety net",
                    tenant.id,
                )

            # Eagerly sync cron payloads against Postgres-canonical. Suspended
            # tenants don't run apply_pending_configs (they're filtered out by
            # the ACTIVE-status gate), so any drift accumulated before
            # suspension stays frozen in OpenClaw runtime. PR #532's Layer 2
            # diff would catch it lazily at the next post-reactivation sweep,
            # but that's up to an hour of "first cron fire might silently
            # error at preflight." Doing it here eagerly closes the window —
            # the canary 2026-05-12 incident had Evening Check-in failing for
            # multiple days because the stale `payload.model` was never
            # caught at reactivation time.
            try:
                from apps.orchestrator.services import refresh_system_cron_rows_from_seed

                drift_result = refresh_system_cron_rows_from_seed(tenant)
                logger.info(
                    "Reactivation cron-row refresh for tenant %s: created=%d updated=%d preserved_custom=%d",
                    tenant.id,
                    drift_result.get("created", 0),
                    drift_result.get("updated", 0),
                    drift_result.get("preserved_custom", 0),
                )
                # Postgres-row saves fire the post_save signal, which
                # enqueues ``regenerate_tenant_crons`` on a 30s debounce.
                # The reconciler pushes payload/schedule drift to OpenClaw.
                # If the refresh is a no-op (the common case — rows already
                # match seed because suspension only touched the gateway),
                # the signal does not fire; in that path the explicit
                # ``resume_tenant_crons`` enqueue above is what re-enables
                # the disabled crons, and the hourly reconcile sweep is
                # the residual safety net.
            except Exception:
                # Don't block reactivation on a refresh failure. The next
                # apply_pending_configs sweep is the safety net.
                logger.exception(
                    "Reactivation cron-row refresh failed for tenant %s — "
                    "next apply_pending_configs sweep will catch drift",
                    tenant.id,
                )
        except Exception:
            logger.exception(
                "Failed to wake container %s for tenant %s — may need re-provisioning", tenant.container_id, tenant.id
            )

    if should_provision:
        publish_task("provision_tenant", str(tenant.id))
        logger.info("Triggered provisioning for tenant %s", tenant.id)


def handle_subscription_deleted(subscription_data: dict) -> None:
    """Handle customer.subscription.deleted webhook.

    If the tenant was marked pending_deletion (user requested account deletion),
    finalize the hard-delete now that their paid period has ended.
    Otherwise treat it as a normal subscription cancellation (deprovision only).
    """
    from apps.cron.publish import publish_task

    tenant = _find_tenant_for_stripe_event(subscription_data)
    if not tenant:
        logger.error("No tenant found for customer.subscription.deleted payload")
        return

    if tenant.status in (Tenant.Status.DEPROVISIONING, Tenant.Status.DELETED):
        logger.info("Tenant %s already deprovisioning/deleted", tenant.id)
        return

    if tenant.pending_deletion:
        # User requested account deletion — paid period is now over, finalize it.
        # Explicit user intent overrides exempt-tenant protection.
        logger.info(
            "Finalizing scheduled account deletion for tenant %s (subscription ended)",
            tenant.id,
        )
        try:
            from apps.tenants.views import _do_hard_delete

            user = tenant.user
            _do_hard_delete(user)
        except Exception:
            logger.exception("Hard-delete failed for tenant %s after subscription ended", tenant.id)
        return

    if tenant.is_budget_exempt:
        # Infrastructure-class tenant (canary, internal accounts) — never
        # auto-deprovision on a subscription event. These tenants exist
        # outside the normal billing lifecycle: their Stripe state may
        # cycle through test events, manual cancels, or expired trials
        # without any signal that the underlying tenant should be torn
        # down. ``is_budget_exempt`` is the existing "off-billing-rails"
        # marker; reusing it avoids a new flag.
        #
        # Explicit ``pending_deletion`` requests still flow through above,
        # so an exempt tenant can still be deleted on purpose.
        logger.info(
            "Skipping subscription-cancel deprovision for budget-exempt tenant %s "
            "(stripe_customer=%s, stripe_subscription=%s) — flip is_budget_exempt=False "
            "if you want this tenant to follow the normal billing lifecycle.",
            tenant.id,
            tenant.stripe_customer_id,
            tenant.stripe_subscription_id,
        )
        return

    # Normal cancellation — deprovision container but keep user record.
    tenant.status = Tenant.Status.DEPROVISIONING
    tenant.save(update_fields=["status", "updated_at"])

    publish_task("deprovision_tenant", str(tenant.id))
    logger.info("Triggered deprovisioning for tenant %s", tenant.id)


def handle_invoice_payment_failed(invoice_data: dict) -> None:
    """Handle invoice.payment_failed webhook by suspending tenant access.

    Scales the container to zero replicas to stop burning Azure resources
    while keeping the container available for fast reactivation if the
    user pays later.
    """
    tenant = _find_tenant_for_stripe_event(invoice_data)
    if not tenant:
        logger.error("No tenant found for invoice.payment_failed payload")
        return

    if tenant.status == Tenant.Status.SUSPENDED:
        logger.info("Tenant %s already paused (payment lapsed)", tenant.id)
        return

    # Disable cron jobs before suspending (container must be reachable)
    if tenant.container_fqdn:
        try:
            from apps.cron.suspension import suspend_tenant_crons

            cron_result = suspend_tenant_crons(tenant)
            logger.info(
                "Disabled %d cron jobs for tenant %s before suspension",
                cron_result.get("disabled", 0),
                tenant.id,
            )
        except Exception:
            logger.exception("Failed to suspend crons for tenant %s", tenant.id)

    tenant.status = Tenant.Status.SUSPENDED
    tenant.hibernated_at = None  # Billing suspension supersedes idle hibernation
    tenant.save(update_fields=["status", "hibernated_at", "updated_at"])
    logger.warning("Paused tenant %s after failed invoice", tenant.id)

    # Hibernate the container (scale to zero) to stop resource costs
    if tenant.container_id:
        try:
            from apps.orchestrator.azure_client import scale_container_app

            scale_container_app(tenant.container_id, min_replicas=0, max_replicas=0)
            logger.info("Hibernated container %s for suspended tenant %s", tenant.container_id, tenant.id)
        except Exception:
            logger.exception("Failed to hibernate container %s for tenant %s", tenant.container_id, tenant.id)
