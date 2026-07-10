"""Month-close donation ledger — turn the platform's revenue pledge into a
recorded, auditable, manually-disbursed ``DonationLedger`` row.

The pledge: 10% (owner-tunable) of all collected revenue — subscriptions AND
credit top-ups — goes to food initiatives. Once a month, for the just-closed
month, we write one ledger row per tenant whose revenue base is
``subscription_price + net_topup_revenue`` (only the pieces we can verify were
actually collected), holding ``revenue_base * DONATION_REVENUE_PCT / 100``.
Disbursement is manual (MVP): a human donates and flips ``pending → completed``
with a receipt via the ``reconcile_donations`` management command — money only
ever moves out of band.

Model note — this is a PLATFORM commitment, not a user-routed choice, so the
per-tenant ``donation_enabled`` / ``donation_percentage`` fields on ``Tenant`` do
NOT gate it: every paying subscriber and every top-up buyer contributes to the
pledge regardless of those toggles. Those fields are left intact for the
transparency dashboard's settings UI; they simply don't drive this ledger.

Why revenue-% and not surplus: the prior design donated ``price − usage − infra``,
which paradoxically donated LESS the more a subscriber used the product and
depended on fragile Azure cost attribution (a heavy month could zero the surplus
→ zero donation). A flat percentage of collected revenue is stable, honest, and
independent of usage.

Top-up revenue is cash-basis: a refund/dispute clawback reduces the month it
lands in, not the month of the original grant, and a refund-heavy month floors
at $0 rather than going negative. Only Stripe-verified money — a subscription
price fetched from Stripe, or a top-up's actual charged amount / server-defined
pack price — may ever create a PENDING amount; anything unverifiable is
recorded SKIPPED. Because ``update_or_create`` is idempotent, re-running the
snapshot after Stripe recovers upgrades a previously SKIPPED-for-unverified row
to PENDING; ``completed`` rows are never rewritten.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from .constants import CREDIT_PACKS

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


def _month_window(month_first: date) -> tuple[datetime, datetime]:
    """Half-open ``[start, end)`` UTC datetime window for the calendar month
    starting on ``month_first``, for filtering ``DateTimeField`` timestamps."""
    if month_first.month == 12:
        next_month_first = date(month_first.year + 1, 1, 1)
    else:
        next_month_first = date(month_first.year, month_first.month + 1, 1)
    return (
        datetime.combine(month_first, time.min, tzinfo=UTC),
        datetime.combine(next_month_first, time.min, tzinfo=UTC),
    )


def _month_topup_revenue(tenant, month_first: date) -> Decimal:
    """Net top-up revenue in USD actually collected from ``tenant`` in the
    calendar month starting ``month_first`` — cash basis, so a refund/dispute
    clawback reduces the month it lands in rather than the month of the
    original grant. Grants and refunds are netted in the SAME unit —
    collected-revenue dollars, never the credit-dollar value of the pack (the
    two differ by the pack's margin, e.g. $6.00 charged for $5.00 of credit).

    Each ``GRANT`` row prefers ``amount_paid_cents`` (what Stripe actually
    charged); when that's NULL it falls back to the pack's server-defined
    ``price_cents`` via the row's ``pack_id`` (still server-authoritative,
    logged loudly) and a grant with neither is skipped with a warning — the
    truth rule is to never guess revenue. Each ``REVERSAL`` row (already
    stored negative, in credit dollars — see ``credits.handle_credit_refund``)
    is matched back to its original grant by ``stripe_payment_intent_id`` and
    converted to revenue dollars via the refunded *fraction* of that grant
    (``abs(reversal.amount) / grant.amount``, clamped to ``[0, 1]``) applied to
    the grant's own resolved revenue — so a fully-refunded pack claws back the
    full price charged, not just the smaller credit value. A reversal with no
    matching grant, or whose grant's revenue can't be resolved either, falls
    back to netting the raw credit-dollar clawback directly (under-subtracts
    by the pack margin, which errs toward donating slightly MORE, never less).
    The result is floored at ``Decimal("0")`` — a refund-heavy month must never
    produce a negative donation base — with a warning logged when flooring
    kicks in.
    """
    from .models import CreditLedger

    window_start, window_end = _month_window(month_first)
    revenue = Decimal("0")

    def _grant_revenue(grant) -> Decimal | None:
        """Resolve a GRANT row's actual collected-revenue dollars, or None if
        unresolvable. Shared by the grant-counting loop below and the
        refund-matching loop, so both sides value the same grant identically."""
        if grant.amount_paid_cents is not None:
            return Decimal(grant.amount_paid_cents) / Decimal("100")
        pack = CREDIT_PACKS.get(grant.pack_id or "")
        if pack:
            logger.warning(
                "Donation snapshot: top-up grant %s (tenant %s) missing amount_paid_cents "
                "— falling back to pack price for pack_id=%s",
                grant.id,
                tenant.id,
                grant.pack_id,
            )
            return Decimal(pack["price_cents"]) / Decimal("100")
        logger.warning(
            "Donation snapshot: top-up grant %s (tenant %s) has neither "
            "amount_paid_cents nor a resolvable pack_id (%r) — skipping it "
            "(truth rule: never guess revenue).",
            grant.id,
            tenant.id,
            grant.pack_id,
        )
        return None

    grants = CreditLedger.objects.filter(
        tenant=tenant,
        kind=CreditLedger.Kind.GRANT,
        created_at__gte=window_start,
        created_at__lt=window_end,
    )
    for grant in grants:
        grant_revenue = _grant_revenue(grant)
        if grant_revenue is not None:
            revenue += grant_revenue

    reversals = CreditLedger.objects.filter(
        tenant=tenant,
        kind=CreditLedger.Kind.REVERSAL,
        created_at__gte=window_start,
        created_at__lt=window_end,
    )
    for reversal in reversals:
        refunded_credit = abs(reversal.amount)
        matching_grant = None
        if reversal.stripe_payment_intent_id:
            # Not window-limited — the original grant may have landed in an
            # earlier month than the refund. Mirrors the lookup in
            # credits.handle_credit_refund (oldest grant on the PI).
            matching_grant = (
                CreditLedger.objects.filter(
                    tenant=tenant,
                    kind=CreditLedger.Kind.GRANT,
                    stripe_payment_intent_id=reversal.stripe_payment_intent_id,
                )
                .order_by("created_at")
                .first()
            )

        if matching_grant is None:
            # No grant to match against at all — the raw credit-dollar
            # clawback is the only signal we have. This under-subtracts
            # (credit dollars < revenue dollars, by the pack margin), which
            # errs toward donating slightly MORE than truly collected — never
            # less.
            logger.warning(
                "Donation snapshot: refund clawback %s (tenant %s) has no resolvable "
                "matching grant revenue — netting the raw credit-dollar clawback "
                "instead (under-subtracts by the pack margin).",
                reversal.id,
                tenant.id,
            )
            revenue -= refunded_credit
            continue

        grant_revenue = None
        if matching_grant.amount and matching_grant.amount > 0:
            grant_revenue = _grant_revenue(matching_grant)
        if grant_revenue is None:
            # A matching grant EXISTS but its revenue is unresolvable, which
            # means it contributed $0 to the base above — subtracting the raw
            # clawback here would take money away from OTHER grants' revenue,
            # the one donate-LESS path. Skip this reversal instead: the
            # grant/reversal pair then nets $0 on both sides, symmetric.
            logger.warning(
                "Donation snapshot: refund clawback %s (tenant %s) matches grant "
                "%s whose revenue is unresolvable — skipping this reversal so the "
                "pair nets $0 on both sides instead of subtracting from others.",
                reversal.id,
                tenant.id,
                matching_grant.id,
            )
            continue

        frac = max(Decimal("0"), min(Decimal("1"), refunded_credit / matching_grant.amount))
        revenue -= frac * grant_revenue

    result = revenue.quantize(Decimal("0.0001"))
    if result < 0:
        logger.warning(
            "Donation snapshot: top-up revenue for tenant %s in %s went negative "
            "($%s) — refunds exceeded grants collected this month; flooring at 0.",
            tenant.id,
            month_first.isoformat(),
            result,
        )
        return Decimal("0")
    return result


def snapshot_donations_for_month(month_first: date | None = None) -> dict:
    """Write a ``DonationLedger`` row per tenant with real, verified revenue this
    month (idempotent; a ``completed`` row is never rewritten).

    The row's revenue base combines two independently-verified pieces: the
    subscription price (Stripe-verified, via ``compute_donation``) and net
    top-up revenue (``_month_topup_revenue`` — cash basis, refunds land in the
    month they happen, floored at $0). Only Stripe-verified money ever counts
    toward a PENDING pledge: an unverified subscription price contributes $0 to
    the counted base, and when the tenant also has no top-up activity that
    month its row still records the raw unverified numbers as SKIPPED with a
    warning — exactly the behavior before top-ups existed. Budget-exempt
    tenants are skipped entirely (counted + info-logged). Below-threshold
    donations are recorded ``SKIPPED``. Because ``update_or_create`` is
    idempotent, re-running the snapshot after Stripe recovers upgrades a
    previously ``SKIPPED``-for-unverified row to ``PENDING``; ``completed``
    rows are still never rewritten — and neither is an already-``PENDING`` row
    ever rewritten DOWNWARD: a closed month's real revenue never decreases, so
    a lower recompute (e.g. a Stripe outage after a prior verified run) is a
    verification regression, not new truth, and the existing row is left
    untouched with a loud warning instead.

    ``month_first`` defaults to the first day of the previous month. Returns a
    summary dict for cron logging.
    """
    from apps.tenants.models import Tenant

    from .models import CreditLedger, DonationLedger

    if month_first is None:
        month_first = _closed_month_first(timezone.now().date())

    pending = skipped = completed_seen = exempt = 0
    total_pending = Decimal("0")

    window_start, window_end = _month_window(month_first)
    topup_tenant_ids = (
        CreditLedger.objects.filter(
            kind__in=[CreditLedger.Kind.GRANT, CreditLedger.Kind.REVERSAL],
            created_at__gte=window_start,
            created_at__lt=window_end,
        )
        .values_list("tenant_id", flat=True)
        .distinct()
    )

    # Candidate set: anyone holding a Stripe subscription id, UNION anyone with
    # top-up grant/clawback activity this month (a trial tenant who bought
    # credit still owes its share on that real collected revenue). The
    # subscription price still has to clear ``_is_paying_subscriber`` +
    # Stripe-verification below; this only widens who gets considered. NOT
    # filtered by ``donation_enabled`` — the revenue pledge is a platform
    # commitment, not a per-user opt-in.
    candidates = Tenant.objects.filter(Q(stripe_subscription_id__gt="") | Q(id__in=topup_tenant_ids))

    pledge_pct = _donation_percentage()
    pledge_pct_int = int(round(pledge_pct))

    for tenant in candidates:
        if tenant.is_budget_exempt:
            exempt += 1
            logger.info("Donation snapshot: skipping budget-exempt tenant %s", tenant.id)
            continue

        existing = DonationLedger.objects.filter(tenant=tenant, month=month_first).first()
        if existing and existing.status == DonationLedger.Status.COMPLETED:
            completed_seen += 1
            continue  # already disbursed — never rewrite an auditable record

        topup_revenue = _month_topup_revenue(tenant, month_first)
        is_subscriber = _is_paying_subscriber(tenant)

        sub_revenue_base = Decimal("0")
        sub_price_source = None
        if is_subscriber:
            sub_revenue_base, _sub_donation, _sub_pct, sub_price_source = compute_donation(tenant)
            if sub_price_source != "stripe":
                # Unverified price (Stripe unavailable or a stale/test-mode sub
                # id) — the subscription component doesn't count this run.
                logger.warning(
                    "Donation snapshot: unverified subscription price for tenant %s "
                    "(source=%s) — excluding the subscription component this run; "
                    "re-run after Stripe recovers to include it.",
                    tenant.id,
                    sub_price_source,
                )

        if not is_subscriber and topup_revenue <= 0:
            # Contributes nothing this month (never subscribed / still
            # trialing, no top-ups) — no row, exactly as before top-ups existed.
            skipped += 1
            continue

        if is_subscriber and sub_price_source != "stripe" and topup_revenue <= 0:
            # Unverified sub price, no top-up activity: keep the exact legacy
            # SKIPPED-with-warning row (raw unverified numbers, for audit only
            # — never PENDING against revenue we couldn't verify was collected).
            revenue_base = sub_revenue_base
            donation = (revenue_base * pledge_pct / Decimal("100")).quantize(Decimal("0.0001"))
            status = DonationLedger.Status.SKIPPED
        else:
            counted_sub = sub_revenue_base if sub_price_source == "stripe" else Decimal("0")
            revenue_base = (counted_sub + topup_revenue).quantize(Decimal("0.0001"))
            donation = (revenue_base * pledge_pct / Decimal("100")).quantize(Decimal("0.0001"))
            status = (
                DonationLedger.Status.PENDING
                if revenue_base > 0 and donation >= DONATION_MIN_THRESHOLD
                else DonationLedger.Status.SKIPPED
            )

        if existing and existing.status == DonationLedger.Status.PENDING and revenue_base < existing.surplus_amount:
            # A closed month's true revenue never decreases — a lower
            # recompute against an already-verified PENDING row is a
            # verification regression (e.g. a Stripe price-cache outage
            # after a verified run), never new truth. Keep the existing row
            # untouched rather than quietly rewriting it downward.
            logger.warning(
                "Donation snapshot: recomputed revenue_base ($%s) for tenant %s in %s "
                "is LOWER than the existing PENDING row's surplus_amount ($%s) — "
                "keeping the existing verified row untouched.",
                revenue_base,
                tenant.id,
                month_first.isoformat(),
                existing.surplus_amount,
            )
            pending += 1
            total_pending += existing.donation_amount
            continue

        DonationLedger.objects.update_or_create(
            tenant=tenant,
            month=month_first,
            defaults={
                # ``surplus_amount`` is reused as the revenue base the donation
                # was computed from (subscription + top-ups). Under the
                # revenue-% model there is no surplus term; keeping the field
                # populated makes each row self-auditable: base * pct% == donation.
                "surplus_amount": revenue_base,
                "donation_amount": donation,
                "donation_percentage": pledge_pct_int,
                "status": status,
            },
        )
        if status == DonationLedger.Status.PENDING:
            pending += 1
            total_pending += donation
        else:
            skipped += 1

    logger.info(
        "Donation ledger for %s: %d pending ($%s), %d skipped, %d already completed, %d exempt",
        month_first.isoformat(),
        pending,
        total_pending.quantize(Decimal("0.01")),
        skipped,
        completed_seen,
        exempt,
    )
    return {
        "month": month_first.isoformat(),
        "pending": pending,
        "pending_total": float(total_pending),
        "skipped": skipped,
        "already_completed": completed_seen,
        "exempt": exempt,
    }
