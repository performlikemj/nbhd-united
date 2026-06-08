"""Prepaid-credit core: grants, refunds, overage debits, and API state.

Money-safety invariants (see CONTINUITY_credits.md + the design critique):
- Grants/refunds are idempotent on the Stripe event id via DB unique constraints
  (CreditLedger partial uniques) — Stripe redelivers and may deliver concurrently.
- The granted/refunded amount is ALWAYS re-derived server-side from CREDIT_PACKS,
  never trusted from client input or session metadata.
- Per-turn debits are atomic + race-safe across all channels (conditional UPDATE),
  never drive the balance negative, and favour the customer on contention (a
  reconcile pass trues up against OpenRouter provider truth).
- Purchased credit EXTENDS the included monthly allowance; it is kept separate
  from is_over_budget/effective_cost_budget so the included-allowance threshold
  emails and the OpenRouter 402 breaker keep their meaning.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest

from apps.billing.constants import CREDIT_PACKS, TIER_COST_BUDGETS
from apps.billing.models import CreditLedger
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.0001")


def _effective_cap(monthly_cost_budget, model_tier: str) -> Decimal:
    """Mirror of Tenant.effective_cost_budget for a .values() row. 0 = unlimited."""
    if monthly_cost_budget and monthly_cost_budget > 0:
        return Decimal(str(monthly_cost_budget))
    budget = TIER_COST_BUDGETS.get(model_tier, 5.00)
    return Decimal(str(budget)) if budget else Decimal("0")


# ── Overage debit (shared by record_usage + reconcile) ─────────────────────


def debit_overage_credit(
    tenant_id,
    prior_estimated: Decimal,
    new_estimated: Decimal,
    included_cap: Decimal,
    *,
    description: str = "usage",
    _retry: bool = True,
) -> Decimal:
    """Draw the part of (prior_estimated, new_estimated] that lands ABOVE the
    included cap from the tenant's purchased credit. Atomic, race-safe, and
    idempotent across reconcile passes (a pass where new <= prior draws 0).
    Never goes negative; returns the amount actually drawn.
    """
    if not included_cap or included_cap <= 0:
        return Decimal("0")
    overage_after = max(Decimal("0"), new_estimated - included_cap)
    overage_before = max(Decimal("0"), prior_estimated - included_cap)
    draw = overage_after - overage_before
    if draw <= 0:
        return Decimal("0")

    balance = Tenant.objects.filter(id=tenant_id).values_list("purchased_credit", flat=True).first()
    if not balance or balance <= 0:
        return Decimal("0")
    actual = min(draw, balance)
    # Conditional decrement: only applies if the balance still covers `actual`,
    # so concurrent turns can't drive it negative. On a lost race, retry once
    # with a fresh read; if it still loses, draw nothing (reconcile trues up).
    updated = Tenant.objects.filter(id=tenant_id, purchased_credit__gte=actual).update(
        purchased_credit=F("purchased_credit") - actual
    )
    if not updated:
        if _retry:
            return debit_overage_credit(
                tenant_id, prior_estimated, new_estimated, included_cap, description=description, _retry=False
            )
        return Decimal("0")
    CreditLedger.objects.create(
        tenant_id=tenant_id,
        kind=CreditLedger.Kind.DEBIT,
        amount=-actual,
        description=description,
    )
    return actual


def debit_overage_for_turn(tenant_id, cost: Decimal) -> Decimal:
    """record_usage entry point: draw this turn's above-cap cost from credit.

    Reads the post-increment running total (estimated_cost already includes this
    turn's cost) and attributes THIS turn's overage as overage(post) -
    overage(post - cost), so concurrent turns each attribute their own slice.
    Skips budget-exempt tenants and tenants with no credit.
    """
    row = (
        Tenant.objects.filter(id=tenant_id)
        .values(
            "estimated_cost_this_month", "monthly_cost_budget", "model_tier", "is_budget_exempt", "purchased_credit"
        )
        .first()
    )
    if not row or row["is_budget_exempt"] or (row["purchased_credit"] or 0) <= 0:
        return Decimal("0")
    cap = _effective_cap(row["monthly_cost_budget"], row["model_tier"])
    post = row["estimated_cost_this_month"] or Decimal("0")
    return debit_overage_credit(tenant_id, post - cost, post, cap, description="usage")


# ── Grants / refunds (Stripe webhook side, idempotent) ─────────────────────


def grant_credit(
    *,
    tenant: Tenant,
    credit_dollars: Decimal,
    stripe_event_id: str,
    stripe_session_id: str = "",
    stripe_payment_intent_id: str = "",
    pack_id: str = "",
    amount_paid_cents: int | None = None,
    description: str = "Stripe top-up",
) -> bool:
    """Idempotently apply a paid top-up. Returns True if applied now, False if a
    duplicate (the partial unique on stripe_event_id is the real lock)."""
    if not stripe_event_id:
        logger.error("grant_credit called without stripe_event_id — refusing (would break idempotency)")
        return False
    try:
        with transaction.atomic():
            CreditLedger.objects.create(
                tenant=tenant,
                kind=CreditLedger.Kind.GRANT,
                amount=credit_dollars,
                stripe_event_id=stripe_event_id,
                stripe_session_id=stripe_session_id,
                stripe_payment_intent_id=stripe_payment_intent_id,
                pack_id=pack_id,
                amount_paid_cents=amount_paid_cents,
                description=description,
            )
            Tenant.objects.filter(id=tenant.id).update(purchased_credit=F("purchased_credit") + credit_dollars)
    except IntegrityError:
        logger.info("credit grant already applied (event=%s tenant=%s)", stripe_event_id, str(tenant.id)[:8])
        return False
    logger.info("granted %s credit to tenant %s (event=%s)", credit_dollars, str(tenant.id)[:8], stripe_event_id)
    return True


def refund_credit(
    *,
    tenant: Tenant,
    refund_dollars: Decimal,
    stripe_event_id: str,
    stripe_payment_intent_id: str = "",
    description: str = "Refund clawback",
) -> bool:
    """Idempotently claw back credit on a refund/dispute, clamped at 0 (the
    platform eats any already-spent value — bounded by small pack sizes)."""
    if not stripe_event_id or refund_dollars <= 0:
        return False
    try:
        with transaction.atomic():
            CreditLedger.objects.create(
                tenant=tenant,
                kind=CreditLedger.Kind.REVERSAL,
                amount=-refund_dollars,
                stripe_event_id=stripe_event_id,
                stripe_payment_intent_id=stripe_payment_intent_id,
                description=description,
            )
            Tenant.objects.filter(id=tenant.id).update(
                purchased_credit=Greatest(F("purchased_credit") - refund_dollars, Value(Decimal("0")))
            )
    except IntegrityError:
        logger.info("credit reversal already applied (event=%s)", stripe_event_id)
        return False
    logger.warning(
        "clawed back %s credit from tenant %s (refund event=%s)", refund_dollars, str(tenant.id)[:8], stripe_event_id
    )
    return True


# ── Stripe webhook handlers (called from views.stripe_webhook) ─────────────


def _resolve_tenant(session_data: dict, meta: dict) -> Tenant | None:
    tid = meta.get("tenant_id") or session_data.get("client_reference_id") or ""
    if tid:
        t = Tenant.objects.filter(id=tid).first()
        if t:
            return t
    customer = session_data.get("customer")
    if customer:
        t = Tenant.objects.filter(stripe_customer_id=customer).first()
        if t:
            return t
    return None


def handle_credit_topup_completed(event_id: str, session_data: dict) -> None:
    """Grant credit for a paid one-time top-up Checkout Session. Idempotent.

    Returns silently (caller returns HTTP 200) on unprocessable events — unpaid,
    unknown pack, no tenant — so Stripe stops retrying permanently-bad events.
    """
    if session_data.get("payment_status") != "paid":
        logger.info("credit topup not paid yet (event=%s status=%s)", event_id, session_data.get("payment_status"))
        return
    meta = session_data.get("metadata") or {}
    pack = CREDIT_PACKS.get(meta.get("pack_id", ""))
    if not pack:
        logger.warning("credit topup: unknown pack_id=%r (event=%s)", meta.get("pack_id"), event_id)
        return
    tenant = _resolve_tenant(session_data, meta)
    if not tenant:
        logger.error("credit topup: no tenant resolved (event=%s)", event_id)
        return

    grant_credit(
        tenant=tenant,
        credit_dollars=pack["credit_dollars"],
        stripe_event_id=event_id,
        stripe_session_id=session_data.get("id", ""),
        stripe_payment_intent_id=session_data.get("payment_intent", "") or "",
        pack_id=meta.get("pack_id", ""),
        amount_paid_cents=session_data.get("amount_total"),
        description=f"Top-up: {pack['label']}",
    )

    customer = session_data.get("customer")
    if customer and not tenant.stripe_customer_id:
        Tenant.objects.filter(id=tenant.id, stripe_customer_id="").update(stripe_customer_id=customer)


def handle_credit_refund(event_id: str, charge_data: dict) -> None:
    """Claw back credit proportional to a refund. Matches the grant by
    PaymentIntent (refund/dispute events carry the PI, not session metadata)."""
    pi = charge_data.get("payment_intent") or ""
    if not pi:
        return
    grant = (
        CreditLedger.objects.filter(kind=CreditLedger.Kind.GRANT, stripe_payment_intent_id=pi)
        .order_by("created_at")
        .first()
    )
    if not grant or grant.tenant_id is None:
        logger.info("refund: no matching credit grant for PI=%s (event=%s)", pi, event_id)
        return
    amount = charge_data.get("amount") or 0
    refunded = charge_data.get("amount_refunded") or 0
    if amount <= 0 or refunded <= 0:
        return
    frac = Decimal(refunded) / Decimal(amount)
    refund_dollars = (grant.amount * frac).quantize(_CENTS)
    tenant = Tenant.objects.filter(id=grant.tenant_id).first()
    if not tenant:
        return
    refund_credit(
        tenant=tenant,
        refund_dollars=refund_dollars,
        stripe_event_id=event_id,
        stripe_payment_intent_id=pi,
        description="Refund clawback",
    )


# ── API state (for the Credits page) ───────────────────────────────────────


def _money(value) -> str:
    """Stable 4dp string for money fields (consistent regardless of whether the
    Decimal came fresh from the DB or in-memory)."""
    return str(Decimal(value).quantize(_CENTS))


def credits_state(tenant: Tenant) -> dict:
    cap = tenant.effective_cost_budget
    used = tenant.estimated_cost_this_month or Decimal("0")
    entries = CreditLedger.objects.filter(tenant=tenant).order_by("-created_at")[:20]
    return {
        "purchased_credit": _money(tenant.purchased_credit),
        "included_budget": _money(cap),
        "included_used": _money(used),
        "included_remaining": (_money(max(Decimal("0"), cap - used)) if cap > 0 else None),
        "packs": [
            {
                "id": pid,
                "label": p["label"],
                "price_display": f"${p['price_cents'] / 100:.2f}",
                "credit_display": f"${p['credit_dollars']}",
            }
            for pid, p in CREDIT_PACKS.items()
        ],
        "recent_entries": [
            {
                "kind": e.kind,
                "amount": str(e.amount),
                "description": e.description,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ],
    }
