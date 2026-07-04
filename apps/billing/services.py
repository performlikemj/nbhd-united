"""Billing services — Stripe webhook handling and usage tracking."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db.models import F

from apps.tenants.models import Tenant

from .constants import BYO_MODEL_DISPLAY, MODEL_RATES
from .constants import DEFAULT_RATE as MODELS_DEFAULT_RATE
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


def resolve_tenant_primary_model(tenant: Tenant) -> str:
    """Return the chat-primary model id this tenant's turns should bill to.

    Chain: ``applied_model`` (what the container is actually running with
    after the last reconciliation) → ``preferred_model`` (user/admin
    override that may not yet be applied) → ``TIER_MODELS[tier]["primary"]``
    (tier default). Empty string only if all three are missing — should be
    impossible in practice but caller is expected to defend against it.

    Used by the chat-completions request builders to populate
    ``payload.model`` (so OpenClaw 5.7+ echoes a real model id back at
    top-level), and by ``resolve_model_for_attribution`` as the final
    fallback when the response itself carries no upstream id.
    """
    primary = getattr(tenant, "applied_model", "") or getattr(tenant, "preferred_model", "")
    if primary:
        return primary
    # Lazy import — config_generator pulls in a chunk of the orchestrator
    # graph; deferring keeps billing/services importable from migrations.
    try:
        from apps.orchestrator.config_generator import TIER_MODELS

        tier = getattr(tenant, "model_tier", "starter") or "starter"
        return TIER_MODELS.get(tier, TIER_MODELS["starter"])["primary"]
    except Exception:
        logger.exception("tier-default primary lookup failed for tenant=%s", getattr(tenant, "id", ""))
        return ""


def resolve_model_for_attribution(tenant: Tenant, result: object) -> str:
    """Resolve the model id to record on a usage row for this tenant + response.

    Prefer the upstream id surfaced by ``extract_model_from_response``. When
    the response is empty / carries only the ``"openclaw"`` placeholder
    (the case for OpenClaw ≥ 4.21 chat-completions responses, which strictly
    follow the OpenAI spec and don't surface the upstream model id), fall
    back to ``resolve_tenant_primary_model`` — we know which model we asked
    for, so attribute the turn to that.

    The fallback is wrong only when OpenClaw's ``runWithModelFallback``
    silently swapped to another model mid-call (provider rate-limit /
    outage). That's the safer error direction: over-attribute to the
    primary (typically the more expensive reasoning model) rather than
    leave the row unattributed and collapse into ``DEFAULT_RATE``.

    Logs at INFO when fallback fires so the rate can be quantified.
    """
    extracted = extract_model_from_response(result)
    if extracted:
        return extracted
    fallback = resolve_tenant_primary_model(tenant)
    if fallback:
        logger.info(
            "model attribution fallback: response carried no upstream id; "
            "attributing to tenant primary tenant=%s model=%s",
            str(getattr(tenant, "id", ""))[:8],
            fallback,
        )
    return fallback


def record_usage(
    tenant: Tenant,
    event_type: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model_used: str = "",
    *,
    is_system: bool = False,
    message_count: int = 1,
) -> UsageRecord:
    """Record a usage event.

    Tenant counters (``estimated_cost_this_month``, ``tokens_this_month``,
    message counts) are updated only for user-attributed events. When
    ``is_system=True`` the row is written and ``MonthlyBudget.spent_dollars``
    is still incremented (the platform still pays for it), but the per-tenant
    counters that drive quota enforcement are left alone. Phase 4's weekly
    reflection synthesis uses this path.

    ``message_count`` (default 1) controls how many user-perceived messages
    this single billing event represents. The cold-start coalescing path
    folds N inbound webhooks into one chat-completion call, but the user
    still typed N messages — pass ``message_count=N`` so the per-tenant
    ``messages_today`` / ``messages_this_month`` counters stay accurate.
    Tokens + cost are NOT multiplied because they reflect actual LLM work
    done (the coalesced prompt was a single inference).

    BYO models (Claude via the Anthropic CLI subscription, future Codex
    via OpenAI's CLI) cost the platform $0 when called from a tenant's
    container — the tenant pays the provider directly via their own
    subscription. For those rows we still write the audit record (with
    ``cost_estimate = 0`` for visibility) and still bump message + token
    counters (the user sent a real message; rate-limit accounting still
    owed), but we DON'T increment ``estimated_cost_this_month`` or
    ``MonthlyBudget.spent_dollars`` — either would falsely trip the
    per-tenant $5 cap or the platform-wide circuit breaker on spend the
    platform never incurred.

    Gate is ``not is_system``: system-side calls (extraction, agenda
    hints, weekly synthesis) hit OpenRouter directly from Django with
    the shared key, so even when they target an ``anthropic/...`` model
    the platform IS paying OR. Those rows keep their computed cost.
    """
    is_byo_user_call = model_used in BYO_MODEL_DISPLAY and not is_system
    if is_byo_user_call:
        cost = Decimal("0")
    else:
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
        msg_increment = message_count if event_type == "message" else 0
        # `cost` is already 0 for BYO; the F-expression is a no-op
        # increment in that case (intentional — keeps the code path
        # uniform and the counter math safe under concurrent writes).
        Tenant.objects.filter(id=tenant.id).update(
            messages_today=F("messages_today") + msg_increment,
            messages_this_month=F("messages_this_month") + msg_increment,
            tokens_this_month=F("tokens_this_month") + total_tokens,
            estimated_cost_this_month=F("estimated_cost_this_month") + cost,
        )

        # Draw any spend beyond the included monthly allowance from prepaid
        # credit. Real platform cost only — BYO user calls already set cost==0
        # (no-op), system rows never reach this block, and the helper skips
        # budget-exempt tenants and those with no credit. Local import keeps the
        # billing.services <-> billing.credits boundary one-directional.
        if cost > 0 and not is_byo_user_call:
            from apps.billing.credits import debit_overage_for_turn

            debit_overage_for_turn(tenant.id, cost)

    # Update global budget — also a no-op for BYO since cost == 0.
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
        fields=[
            "estimated_cost_this_month",
            "monthly_cost_budget",
            "model_tier",
            "is_budget_exempt",
            "purchased_credit",
        ]
    )
    if tenant.is_budget_exempt:
        return ""
    # Single source of truth for "may spend": within the included allowance OR
    # holding prepaid credit. is_over_budget stays PURE (included-cap only) so
    # the threshold emails + OpenRouter 402 breaker keep their meaning — credit
    # is folded in only here, via has_spendable_budget.
    if not tenant.has_spendable_budget:
        return "personal"

    # Global platform budget
    from .models import MonthlyBudget

    today = date.today()
    first_of_month = today.replace(day=1)
    try:
        global_budget = MonthlyBudget.objects.get(month=first_of_month)
        # is_capped is the operator-controlled kill-switch (cap_budget command),
        # independent of the reconcile-driven spend counter. Either the explicit
        # cap OR exhausting the budget engages the global breaker.
        if global_budget.is_capped or (global_budget.remaining is not None and global_budget.remaining <= 0):
            return "global"
    except MonthlyBudget.DoesNotExist:
        pass

    return ""


def restore_tenant_runtime(tenant: Tenant) -> bool:
    """Bring a suspended/hibernated tenant's runtime back online.

    Scales the container to a single replica, queues any config apply
    missed while suspended, re-enables the disabled crons (via QStash so
    the resume fires after the gateway is ready), and eagerly syncs cron
    rows from seed to close the reactivation drift window.

    Extracted verbatim from the Stripe reactivation path (this is what the
    ``should_wake`` branch of ``handle_checkout_completed`` used to run
    inline) so the promo-comeback redemption view can reuse the *identical*
    sequence: a nonpayment-suspended tenant that redeems the offer needs its
    container scaled back up and crons resumed, otherwise it would message
    the bot and get silence. Refactor, not behaviour change — the Stripe
    path calls this and behaves exactly as before.

    Idempotent: ``scale_container_app`` treats an already-min_replicas=1
    container (409 ResourceExistsError) as success, and the config apply /
    ``resume_tenant_crons`` / cron-row refresh are all no-ops when nothing
    changed.

    Returns ``True`` when the container scale-up succeeded (the config /
    cron follow-ups are best-effort and only logged on failure, matching
    the original inline behaviour); ``False`` when there is no container to
    scale or the scale-up itself raised — the caller decides the fallback
    (the Stripe path logs and continues; the redeem view marks the tenant
    hibernated so the next inbound message self-heals via
    ``wake_hibernated_tenant``).
    """
    from apps.cron.publish import publish_task

    if not tenant.container_id:
        return False

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
        return True
    except Exception:
        logger.exception(
            "Failed to wake container %s for tenant %s — may need re-provisioning", tenant.container_id, tenant.id
        )
        return False


def handle_checkout_completed(session_data: dict) -> None:
    """Handle Stripe checkout.session.completed webhook (subscription mode)."""
    from apps.cron.publish import publish_task

    # Defensive: a payment-mode session is a one-time credit top-up (routed to
    # handle_credit_topup_completed in views), never a subscription. Guard here
    # too so a mis-route can't flip the tenant's tier / reprovision.
    if (session_data.get("mode") or "") == "payment":
        logger.info("handle_checkout_completed: ignoring payment-mode session %s", session_data.get("id"))
        return

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

    if should_wake:
        restore_tenant_runtime(tenant)

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


def _send_payment_retry_email(tenant: Tenant, invoice_id: str) -> bool:
    """Send the "payment failed, we'll retry — update your card" email.

    Idempotent per invoice via ``tenant.dunning_notice_invoice_id``: the
    email fires once for a given failed invoice, not on every automatic
    Stripe retry that re-emits ``invoice.payment_failed`` for the same
    invoice. Returns True iff a fresh email was sent.

    Mirrors the send pattern in ``apps.router.billing_quota_handlers``
    (EmailMultiAlternatives + render_to_string templates, DEFAULT_FROM_EMAIL)
    and links to the billing settings page rather than minting a portal
    session so a Stripe API hiccup can't break the notice.
    """
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    user = getattr(tenant, "user", None)
    email = (getattr(user, "email", "") or "").strip()
    if not email:
        logger.info("dunning: retry email skipped tenant=%s reason=no_email", str(tenant.id)[:8])
        return False

    # Already notified about THIS invoice — no duplicate on the next retry.
    if invoice_id and tenant.dunning_notice_invoice_id == invoice_id:
        return False

    ctx = {
        "display_name": (getattr(user, "display_name", "") or "").strip() or "there",
        "billing_url": f"{settings.FRONTEND_URL}/billing",
    }
    try:
        subject = render_to_string("email/dunning/payment_failed_retry_subject.txt", ctx).strip()
        txt_body = render_to_string("email/dunning/payment_failed_retry_body.txt", ctx)
        html_body = render_to_string("email/dunning/payment_failed_retry_body.html", ctx)
        msg = EmailMultiAlternatives(subject=subject, body=txt_body, from_email=None, to=[email])
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
    except Exception:
        logger.exception("dunning: retry email send failed to %s", email)
        return False

    # Record the invoice id so subsequent retries of the same invoice don't
    # re-email. Bump only this field to avoid clobbering concurrent writes.
    Tenant.objects.filter(id=tenant.id).update(dunning_notice_invoice_id=invoice_id)
    tenant.dunning_notice_invoice_id = invoice_id
    logger.info("dunning: retry email sent tenant=%s invoice=%s", str(tenant.id)[:8], invoice_id)
    return True


def handle_invoice_payment_failed(invoice_data: dict) -> None:
    """Handle invoice.payment_failed webhook with a dunning grace period.

    Stripe emits this event on *every* automatic-retry attempt, not just the
    final one. Suspending on the first decline permanently strands a customer
    whose card later clears on a retry (the ``invoice.paid`` handler reactivates
    them, but suspending them in the interim needlessly kills their assistant
    over a transient decline).

    So we only suspend when Stripe has exhausted its retries — i.e. the invoice
    payload carries no ``next_payment_attempt`` (null/absent = terminal). While
    retries remain, we keep the tenant running, log it, and email a one-time
    "payment failed, we'll retry — update your card" notice (idempotent per
    invoice). Suspension scales the container to zero replicas to stop burning
    Azure resources while keeping it available for fast reactivation.
    """
    tenant = _find_tenant_for_stripe_event(invoice_data)
    if not tenant:
        logger.error("No tenant found for invoice.payment_failed payload")
        return

    if tenant.status == Tenant.Status.SUSPENDED:
        logger.info("Tenant %s already paused (payment lapsed)", tenant.id)
        return

    invoice_id = invoice_data.get("id") or ""
    # ``next_payment_attempt`` is a unix timestamp when Stripe will retry, and
    # null/absent once it has given up. A pending retry = grace: don't suspend.
    has_retry_pending = invoice_data.get("next_payment_attempt") not in (None, "", 0)
    if has_retry_pending:
        logger.info(
            "Invoice %s failed for tenant %s but Stripe will retry (next_payment_attempt=%s) "
            "— holding service, sending retry notice",
            invoice_id,
            tenant.id,
            invoice_data.get("next_payment_attempt"),
        )
        try:
            _send_payment_retry_email(tenant, invoice_id)
        except Exception:
            logger.exception("dunning: failed to send retry notice for tenant %s", tenant.id)
        return

    logger.warning(
        "Invoice %s failed for tenant %s with no further Stripe retry — suspending",
        invoice_id,
        tenant.id,
    )

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


def handle_invoice_paid(invoice_data: dict) -> None:
    """Handle invoice.paid / invoice.payment_succeeded — auto-reactivate.

    When Stripe's automatic dunning retry finally succeeds, the customer has
    paid but a tenant we suspended on a *terminal* decline would otherwise stay
    dark forever (only a fresh checkout or portal action woke them). This
    restores service for a subscription invoice belonging to a billing-suspended
    tenant.

    Guards / idempotency:
      - Only subscription invoices reactivate. One-off invoices and credit
        top-ups (checkout mode=payment, no subscription) are ignored.
      - Unknown subscription/customer → logged and ignored.
      - Only a tenant in SUSPENDED status is woken (SUSPENDED is set solely by
        the billing-failure path, so this never wakes a deliberately
        deprovisioned/deleted/pending tenant). ACTIVE tenants → no-op.
      - A tenant queued for deletion (pending_deletion) is never woken.
      - Container scale-up reuses restore_tenant_runtime, whose Azure ops treat
        already-in-requested-state (409/ResourceExistsError) as success.

    Exception-safe: any unexpected error is logged, not raised, so a malformed
    payload can't break the webhook dispatcher.
    """
    try:
        billing_reason = (invoice_data.get("billing_reason") or "").lower()
        subscription_id = invoice_data.get("subscription") or ""
        # Subscription invoices carry a ``subscription`` id (and a
        # ``subscription_*`` billing_reason). Credit top-ups are mode=payment
        # checkouts with no subscription and must not route here.
        is_subscription_invoice = bool(subscription_id) or billing_reason.startswith("subscription")
        if not is_subscription_invoice:
            logger.info(
                "invoice.paid: ignoring non-subscription invoice %s (billing_reason=%s)",
                invoice_data.get("id"),
                billing_reason or "none",
            )
            return

        tenant = _find_tenant_for_stripe_event(invoice_data)
        if not tenant:
            logger.info(
                "invoice.paid: no tenant for subscription=%s customer=%s — ignoring",
                subscription_id,
                invoice_data.get("customer"),
            )
            return

        if tenant.status != Tenant.Status.SUSPENDED:
            logger.info(
                "invoice.paid: tenant %s not suspended (status=%s) — no-op",
                tenant.id,
                tenant.status,
            )
            return

        if tenant.pending_deletion:
            logger.info(
                "invoice.paid: tenant %s is pending deletion — not reactivating",
                tenant.id,
            )
            return

        # Restore billing state and wake the runtime. Mirrors the minimal
        # correct subset of handle_checkout_completed's SUSPENDED branch.
        tenant.status = Tenant.Status.ACTIVE
        tenant.hibernated_at = None  # Clear any idle-hibernation marker
        tenant.is_trial = False
        tenant.dunning_notice_invoice_id = ""  # Re-arm the retry notice for a future decline
        if subscription_id and not tenant.stripe_subscription_id:
            tenant.stripe_subscription_id = subscription_id
        customer_id = invoice_data.get("customer") or ""
        if customer_id and not tenant.stripe_customer_id:
            tenant.stripe_customer_id = customer_id
        tenant.save(
            update_fields=[
                "status",
                "hibernated_at",
                "is_trial",
                "dunning_notice_invoice_id",
                "stripe_subscription_id",
                "stripe_customer_id",
                "updated_at",
            ]
        )
        logger.info(
            "invoice.paid: reactivated billing-suspended tenant %s (invoice=%s)",
            tenant.id,
            invoice_data.get("id"),
        )

        restore_tenant_runtime(tenant)
    except Exception:
        logger.exception("invoice.paid: unexpected error handling payload")
