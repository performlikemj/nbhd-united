"""Hourly reconciliation of per-tenant + platform OpenRouter spend.

Walks every active tenant that has an ``openrouter_key_secret_name`` set
and reads the corresponding sub-key from Key Vault. For each, calls
``GET /api/v1/key`` *with that key as Bearer auth* — OR returns the
key's own ``usage_monthly``. We update ``tenant.estimated_cost_this_month``
upward to ``max(internal, provider)`` so an OR-side spike (the case we
care about) is reflected in the next ``check_budget`` call without
needing a deploy or a manual recompute.

We never reduce the counter — internal accounting may include BYO calls
that OR doesn't see, and a downward edit would let a tenant chat past
the cap. Truing up always overestimates, which is the safe error
direction.

Platform tally: also polls the shared ``OPENROUTER_API_KEY`` for system-
side usage (extraction, agenda hints, weekly synthesis) and writes the
combined per-tenant + shared total into ``MonthlyBudget.spent_dollars``
so the global circuit breaker (returns ``"global"`` from
``check_budget``) reflects provider truth too.

Idempotent. One tenant's failure is logged and the loop continues. Run
hourly via QStash; see ``apps/cron/management/commands/register_system_crons.py``.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import F, Q, Sum

from apps.billing.openrouter_admin import get_key_usage, get_shared_key_usage
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _reconcile_tenant(tenant: Tenant) -> tuple[bool, Decimal, Decimal]:
    """Reconcile a single tenant's ``estimated_cost_this_month`` against
    their OR sub-key's ``usage_monthly``.

    Returns ``(updated, before, after)``.
    """
    from apps.orchestrator.azure_client import read_key_vault_secret

    secret_name = (tenant.openrouter_key_secret_name or "").strip()
    if not secret_name:
        return False, Decimal("0"), Decimal("0")

    api_key = read_key_vault_secret(secret_name)
    if not api_key:
        logger.warning(
            "reconcile_or: tenant=%s no key value at KV secret %s",
            str(tenant.id)[:8],
            secret_name,
        )
        return False, Decimal("0"), Decimal("0")

    provider_truth = get_key_usage(api_key)
    if provider_truth <= 0:
        return False, Decimal("0"), Decimal("0")

    before = Decimal(str(tenant.estimated_cost_this_month or 0))
    if provider_truth <= before:
        return False, before, before

    # Update upward only. F-expression avoids racing with concurrent
    # record_usage increments (a concurrent write between our read and
    # update would otherwise be lost).
    delta = provider_truth - before
    Tenant.objects.filter(id=tenant.id).update(estimated_cost_this_month=F("estimated_cost_this_month") + delta)

    # True prepaid-credit consumption to provider truth too: record_usage's
    # per-turn estimate can under-draw, so draw the reconciled overage delta
    # from credit here. Idempotent across passes — a later pass has
    # provider_truth <= before and returns at the early guard above, so the
    # overage delta (and thus the draw) is 0.
    from apps.billing.credits import debit_overage_credit

    drawn = debit_overage_credit(
        tenant.id, before, provider_truth, tenant.effective_cost_budget, description="reconcile"
    )
    if drawn:
        logger.info("reconcile_or: tenant=%s drew $%.4f from prepaid credit", str(tenant.id)[:8], float(drawn))
    logger.info(
        "reconcile_or: tenant=%s trued up +$%.4f (estimate=%s → provider=%s)",
        str(tenant.id)[:8],
        float(delta),
        before,
        provider_truth,
    )
    return True, before, provider_truth


def _reconcile_platform_total() -> None:
    """Update ``MonthlyBudget.spent_dollars`` to reflect platform-wide truth.

    Platform total = per-tenant OR truth (already trued up above) plus
    the shared-key's ``usage_monthly`` (system-side calls like extraction
    and weekly synthesis that don't go through per-tenant sub-keys).

    BYO Claude calls don't go through OR at all and aren't counted here —
    they're tenant-paid via the Anthropic CLI subscription and don't hit
    the platform's OR balance.
    """
    from apps.billing.models import MonthlyBudget

    per_tenant_total = Tenant.objects.aggregate(s=Sum("estimated_cost_this_month"))["s"] or Decimal("0")
    shared_truth = get_shared_key_usage()
    platform_truth = per_tenant_total + shared_truth

    first_of_month = date.today().replace(day=1)
    budget, _ = MonthlyBudget.objects.get_or_create(
        month=first_of_month,
        defaults={"budget_dollars": 100},
    )
    current_spent = Decimal(str(budget.spent_dollars or 0))
    if platform_truth > current_spent:
        delta = platform_truth - current_spent
        MonthlyBudget.objects.filter(id=budget.id).update(spent_dollars=F("spent_dollars") + delta)
        logger.info(
            "reconcile_or: platform spent_dollars trued up +$%.4f (was %s → now %s)",
            float(delta),
            current_spent,
            platform_truth,
        )


def reconcile_all() -> dict:
    """Run a full reconciliation pass. Returns a summary dict for logging."""
    candidates = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
    ).exclude(Q(openrouter_key_secret_name="") | Q(openrouter_key_secret_name__isnull=True))

    # Lazy import — keeps the management command importable from
    # migrations / Django setup paths that may not have apps.router loaded.
    from apps.router.billing_quota_handlers import fire_threshold_emails_if_crossed

    updated = 0
    failed = 0
    for tenant in candidates:
        try:
            changed, before, after = _reconcile_tenant(tenant)
            if changed:
                updated += 1
                # If the trued-up counter crossed 90% or 100% of the
                # tenant's cap, fire the corresponding email. Idempotent
                # via per-tenant sent-at markers (PR #1.8).
                try:
                    fire_threshold_emails_if_crossed(tenant, before=before, after=after)
                except Exception:
                    logger.exception(
                        "reconcile_or: threshold-email dispatch failed for tenant=%s",
                        str(tenant.id)[:8],
                    )
        except Exception:
            logger.exception("reconcile_or: tenant=%s failed", str(tenant.id)[:8])
            failed += 1

    try:
        _reconcile_platform_total()
    except Exception:
        logger.exception("reconcile_or: platform-total reconciliation failed")
        failed += 1

    summary = {"updated": updated, "failed": failed, "checked": candidates.count()}
    logger.info("reconcile_or: pass complete %s", summary)
    return summary


class Command(BaseCommand):
    help = "Reconcile internal cost counters against OpenRouter provider truth."

    def handle(self, *args, **options):
        result = reconcile_all()
        self.stdout.write(
            self.style.SUCCESS(f"OK: updated={result['updated']} failed={result['failed']} checked={result['checked']}")
        )
