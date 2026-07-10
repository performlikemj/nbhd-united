"""Month-close donation ledger — turn the platform's revenue pledge into a
recorded, auditable, manually-disbursed ``DonationLedger`` row.

The owner's commitment is simple: a small percentage of gross subscription
revenue goes to food initiatives. Once a month, for the just-closed month, we
write one ``pending`` ledger row per *paying* subscriber holding
``subscription_price * DONATION_REVENUE_PCT / 100``. Disbursement is manual
(MVP): a human donates and flips ``pending → completed`` with a receipt via the
``reconcile_donations`` management command — money only ever moves out of band.

Model note — this is a PLATFORM commitment, not a user-routed choice, so the
per-tenant ``donation_enabled`` / ``donation_percentage`` fields on ``Tenant`` do
NOT gate it: every paying subscriber contributes to the pledge regardless of
those toggles. Those fields are left intact for the transparency dashboard's
settings UI; they simply don't drive this ledger.

Why revenue-% and not surplus: the prior design donated ``price − usage − infra``,
which paradoxically donated LESS the more a subscriber used the product and
depended on fragile Azure cost attribution (a heavy month could zero the surplus
→ zero donation). A flat percentage of collected revenue is stable, honest, and
independent of usage.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)

# Donations below this aren't worth recording or disbursing.
DONATION_MIN_THRESHOLD = Decimal("0.01")


def _is_paying_subscriber(tenant) -> bool:
    """True for a tenant who actually pays a subscription — i.e. whose revenue we
    can pledge against. Excludes never-subscribed, comped, and currently-trialing
    tenants (all pay $0) so we never record a donation against revenue that was
    never collected. ``has_entitlement`` is deliberately NOT used here because it
    also returns True for unexpired trials.

    Manual reconciliation is the human backstop for the remaining edge (e.g. a
    lapsed subscription whose ``stripe_subscription_id`` lingers): a person eyes
    the pending list before any money moves.
    """
    if not tenant.stripe_subscription_id:
        return False
    on_valid_trial = bool(tenant.is_trial) and tenant.trial_ends_at and tenant.trial_ends_at > timezone.now()
    return not on_valid_trial


def _closed_month_first(today: date) -> date:
    """First day of the previous (just-closed) month relative to ``today``."""
    if today.month == 1:
        return date(today.year - 1, 12, 1)
    return date(today.year, today.month - 1, 1)


def _donation_percentage() -> Decimal:
    """The platform's pledged percentage of gross revenue (owner-tunable)."""
    from django.conf import settings

    return Decimal(str(getattr(settings, "DONATION_REVENUE_PCT", 10.0)))


def compute_donation(tenant) -> tuple[Decimal, Decimal, int, str]:
    """Return ``(revenue_base, donation, pct, price_source)`` for ``tenant``.

    ``revenue_base`` is the tenant's real monthly subscription price (from
    Stripe, with a safe settings fallback); ``donation`` is that price times the
    platform's pledged percentage; ``pct`` is the pledged percentage as an int
    snapshot recorded on the row; ``price_source`` is ``"stripe"`` when the price
    was verified against Stripe or ``"fallback"`` when the settings default was
    used (no/unpriceable subscription, or Stripe was unavailable). The snapshot
    refuses to book a PENDING pledge against a ``"fallback"`` price. Deliberately
    independent of usage and infra — that is the whole point of the revenue-% model.
    """
    from .usage_services import _get_subscription_price_with_source

    pct = _donation_percentage()
    price, price_source = _get_subscription_price_with_source(tenant)
    revenue_base = Decimal(str(price)).quantize(Decimal("0.0001"))
    donation = (revenue_base * pct / Decimal("100")).quantize(Decimal("0.0001"))
    return revenue_base, donation, int(round(pct)), price_source


def snapshot_donations_for_month(month_first: date | None = None) -> dict:
    """Write a ``DonationLedger`` row per paying subscriber for the just-closed
    month (idempotent; a ``completed`` row is never rewritten).

    Only Stripe-verified subscription prices book a PENDING pledge. When the price
    could only be resolved via the settings fallback (Stripe unavailable, or a
    stale/test-mode subscription id that Stripe won't price) the row is written
    with status ``SKIPPED`` and a warning is logged, so we never pledge real money
    against revenue we couldn't verify was collected. Below-threshold donations are
    also recorded ``SKIPPED`` (unchanged). Because ``update_or_create`` is
    idempotent, re-running the snapshot after Stripe recovers upgrades a previously
    ``SKIPPED``-for-unverified row to ``PENDING`` with the real price; ``completed``
    rows are still never rewritten.

    ``month_first`` defaults to the first day of the previous month. Returns a
    summary dict for cron logging.
    """
    from apps.tenants.models import Tenant

    from .models import DonationLedger

    if month_first is None:
        month_first = _closed_month_first(timezone.now().date())

    pending = skipped = completed_seen = 0
    total_pending = Decimal("0")

    # Candidate set: anyone holding a Stripe subscription id. ``_is_paying_subscriber``
    # then drops still-trialing subscriptions. NOT filtered by ``donation_enabled``
    # — the revenue pledge is a platform commitment, not a per-user opt-in.
    candidates = Tenant.objects.exclude(stripe_subscription_id="").exclude(stripe_subscription_id__isnull=True)

    for tenant in candidates:
        if not _is_paying_subscriber(tenant):
            skipped += 1
            continue

        existing = DonationLedger.objects.filter(tenant=tenant, month=month_first).first()
        if existing and existing.status == DonationLedger.Status.COMPLETED:
            completed_seen += 1
            continue  # already disbursed — never rewrite an auditable record

        revenue_base, donation, pct, price_source = compute_donation(tenant)
        if price_source != "stripe":
            # Unverified price (Stripe unavailable or a stale/test-mode sub id) —
            # book SKIPPED, not PENDING, so we never pledge against unverified
            # revenue. Loud in prod logs; re-run upgrades it once Stripe recovers.
            status = DonationLedger.Status.SKIPPED
            logger.warning(
                "Donation snapshot: unverified subscription price for tenant %s "
                "(source=%s) — recording SKIPPED instead of PENDING; re-run after "
                "Stripe recovers to upgrade this row.",
                tenant.id,
                price_source,
            )
        elif donation >= DONATION_MIN_THRESHOLD:
            status = DonationLedger.Status.PENDING
        else:
            status = DonationLedger.Status.SKIPPED
        DonationLedger.objects.update_or_create(
            tenant=tenant,
            month=month_first,
            defaults={
                # ``surplus_amount`` is reused as the revenue base the donation was
                # computed from (gross monthly subscription price). Under the
                # revenue-% model there is no surplus term; keeping the field
                # populated makes each row self-auditable: base * pct% == donation.
                "surplus_amount": revenue_base,
                "donation_amount": donation,
                "donation_percentage": pct,
                "status": status,
            },
        )
        if status == DonationLedger.Status.PENDING:
            pending += 1
            total_pending += donation
        else:
            skipped += 1

    logger.info(
        "Donation ledger for %s: %d pending ($%s), %d skipped, %d already completed",
        month_first.isoformat(),
        pending,
        total_pending.quantize(Decimal("0.01")),
        skipped,
        completed_seen,
    )
    return {
        "month": month_first.isoformat(),
        "pending": pending,
        "pending_total": float(total_pending),
        "skipped": skipped,
        "already_completed": completed_seen,
    }
