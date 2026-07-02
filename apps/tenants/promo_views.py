"""Promo redemption HTTP surface.

``GET /api/v1/tenants/promos/redeem/?code=<campaign>&token=<signed>`` verifies
the HMAC, checks the campaign deadline, and applies the
trial-extension side effect on the tenant. On success / failure the
view 302s to the frontend success page at
``{FRONTEND_URL}/promo/redeemed?status=<state>`` — the page is a static
Next.js route that reads the query param and renders one of four
copy variations.

The view is unauthenticated by design: clicking from an email inbox
shouldn't require a prior login. Authorization is carried entirely by
the per-user HMAC token (signed in ``promo_signing.py``).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from apps.tenants.models import Tenant, User
from apps.tenants.promo_models import PromoCampaign, PromoRedemption
from apps.tenants.promo_signing import verify_promo_token

logger = logging.getLogger(__name__)


def _redirect(status: str) -> HttpResponseRedirect:
    """302 to the frontend success page with the rendered state."""
    frontend = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
    qs = urlencode({"status": status})
    return HttpResponseRedirect(f"{frontend}/promo/redeemed?{qs}")


@api_view(["GET"])
@permission_classes([AllowAny])
def redeem_promo(request):
    """Verify token + campaign, apply trial extension, redirect to
    the frontend confirmation page.

    Failure modes (each maps to a distinct ``status=`` value the
    frontend renders distinct copy for):

      - ``invalid`` — missing/malformed params, bad HMAC, campaign
        not found, user not found
      - ``expired`` — campaign past ``valid_until``
      - ``already`` — user has already redeemed this campaign
      - ``active_subscription`` — user has an active Stripe sub,
        no trial to extend (defensive — audience filter should keep
        these users out of the send list)
      - ``success`` — trial extended
    """
    code = (request.GET.get("code") or "").strip()
    token = (request.GET.get("token") or "").strip()

    if not code or not token:
        return _redirect("invalid")

    try:
        campaign = PromoCampaign.objects.get(code=code)
    except PromoCampaign.DoesNotExist:
        logger.info("Promo redemption rejected — unknown campaign %s", code)
        return _redirect("invalid")

    if timezone.now() >= campaign.valid_until:
        logger.info("Promo redemption rejected — campaign %s expired", code)
        return _redirect("expired")

    user_id = verify_promo_token(code, token)
    if user_id is None:
        logger.info("Promo redemption rejected — bad token for campaign %s", code)
        return _redirect("invalid")

    try:
        user = User.objects.select_related("tenant").get(id=user_id)
    except (User.DoesNotExist, ValueError):
        # ValueError catches malformed UUIDs from a tampered token that
        # somehow survived signature verification (shouldn't happen,
        # but defensive).
        logger.info("Promo redemption rejected — user %s not found", user_id)
        return _redirect("invalid")

    tenant = getattr(user, "tenant", None)
    if tenant is None:
        # Record the outcome for audit then bail. Idempotent via the
        # unique_together constraint.
        _record_redemption(campaign, user, PromoRedemption.Outcome.NO_TENANT, new_trial_ends_at=None)
        return _redirect("invalid")

    # A genuinely-active paying subscriber shouldn't be flipped to trial.
    # But a SUSPENDED tenant keeps a *retained* stripe_subscription_id even
    # though it isn't paying — nonpayment suspension deliberately holds onto
    # the sub id for fast reactivation (billing.services.handle_invoice_
    # payment_failed). Those paid-then-lapsed tenants are exactly who the
    # comeback offer targets, so let them redeem (extend + restore runtime).
    # Only the ACTIVE-with-subscription case is a real paying subscriber to
    # protect from a trial flip.
    if tenant.status != Tenant.Status.SUSPENDED and tenant.stripe_subscription_id:
        _record_redemption(campaign, user, PromoRedemption.Outcome.ALREADY_SUBSCRIBED, new_trial_ends_at=None)
        return _redirect("active_subscription")

    # Remember whether we're reactivating a nonpayment-suspended tenant
    # *before* we flip the status below — it drives the runtime restore.
    was_suspended = tenant.status == Tenant.Status.SUSPENDED

    # A paid-then-lapsed SUSPENDED tenant still carries the dead Stripe
    # subscription id it was suspended with. We MUST clear it as part of
    # granting the trial (same atomic write below), because leaving it set
    # would permanently defeat trial expiry: `_unentitled_active_tenants()`
    # excludes any tenant with `stripe_subscription_id > ""` from the
    # expire-trials sweep, and `Tenant.has_entitlement` short-circuits True
    # whenever a sub id is present — so the tenant would keep free service
    # forever after trial_ends_at passes. Retaining the id buys nothing:
    # invoice.payment_succeeded / subscription.updated are unhandled no-ops,
    # so no automatic reactivation depends on it. A genuine re-subscription
    # later flows through handle_checkout_completed, which writes a fresh sub
    # id cleanly. We log the cleared id prominently so the rare
    # suspended-but-actually-paying edge case can be reconciled by hand from
    # the Stripe dashboard.
    cleared_sub_id = tenant.stripe_subscription_id if (was_suspended and tenant.stripe_subscription_id) else ""

    # Apply the extension. trial_ends_at = max(now, existing) + days.
    now = timezone.now()
    base = tenant.trial_ends_at if (tenant.trial_ends_at and tenant.trial_ends_at > now) else now
    new_trial_ends_at = base + timedelta(days=campaign.extension_days)

    # Insert the redemption row *first*, outside any wrapping
    # transaction. The unique_together constraint is the second-click
    # race guard — if the second click loses, IntegrityError fires here,
    # we treat it as "already redeemed" and don't touch the tenant.
    # Doing it this way (insert outside, update inside) avoids the
    # Django gotcha where catching IntegrityError inside ``atomic()``
    # leaves the transaction in a broken state for subsequent queries.
    redemption = _record_redemption(
        campaign,
        user,
        PromoRedemption.Outcome.EXTENDED,
        new_trial_ends_at=new_trial_ends_at,
    )
    if redemption is None:
        return _redirect("already")

    try:
        with transaction.atomic():
            tenant.trial_ends_at = new_trial_ends_at
            tenant.is_trial = True
            tenant.status = Tenant.Status.ACTIVE
            update_fields = ["trial_ends_at", "is_trial", "status", "updated_at"]
            if cleared_sub_id:
                tenant.stripe_subscription_id = ""
                update_fields.append("stripe_subscription_id")
            tenant.save(update_fields=update_fields)
    except Exception:
        logger.exception(
            "Promo redemption tenant update failed — campaign=%s user=%s",
            campaign.code,
            user.id,
        )
        # Roll back the redemption row so a retry can re-attempt the
        # full operation cleanly. The unique_together constraint would
        # otherwise block the retry.
        redemption.delete()
        return _redirect("invalid")

    if cleared_sub_id:
        # Prominent audit line: if this tenant was in fact still paying via
        # Stripe (rare — it was SUSPENDED, so it shouldn't be), reconcile the
        # cleared subscription id manually from the Stripe dashboard.
        logger.info(
            "Promo redemption CLEARED dead stripe_subscription_id — campaign=%s tenant=%s user=%s "
            "cleared_sub_id=%s (granted %d-day trial; reconcile in Stripe if this sub was still live)",
            campaign.code,
            tenant.id,
            user.id,
            cleared_sub_id,
            campaign.extension_days,
        )

    # Runtime restore for reactivated tenants. Flipping DB state to ACTIVE
    # is not enough for a nonpayment-suspended tenant — its container sits
    # at 0 replicas with crons disabled, so without this they'd click the
    # offer, message the bot, and get silence. ``restore_tenant_runtime``
    # (the extracted Stripe-reactivation sequence) scales the container back
    # up and resumes crons. It never raises — it swallows Azure failures and
    # returns False — but the page must succeed regardless, so we also guard
    # defensively. On failure we mark the tenant hibernated: the next inbound
    # message then routes through ``handle_hibernated_message`` →
    # ``wake_hibernated_tenant``, which scales the container to 1 and
    # schedules cron resume, so the user self-heals on first contact.
    if was_suspended:
        from apps.billing.services import restore_tenant_runtime

        try:
            restored = restore_tenant_runtime(tenant)
        except Exception:
            logger.exception(
                "Promo redemption — restore_tenant_runtime raised for tenant %s",
                tenant.id,
            )
            restored = False

        if not restored:
            Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())
            logger.warning(
                "Promo redemption — runtime restore failed for tenant %s; marked "
                "hibernated so the next inbound message wakes the container",
                tenant.id,
            )

    logger.info(
        "Promo redemption applied — campaign=%s user=%s new_trial_ends_at=%s",
        campaign.code,
        user.id,
        new_trial_ends_at.isoformat(),
    )
    return _redirect("success")


def _record_redemption(
    campaign: PromoCampaign,
    user: User,
    outcome: str,
    *,
    new_trial_ends_at,
) -> PromoRedemption | None:
    """Insert the audit row. Returns the new row on success, ``None``
    if the unique_together constraint fired (already redeemed).

    The insert is wrapped in a savepoint via ``transaction.atomic()``.
    Without it, an IntegrityError from a parent transaction (e.g. a
    request running inside TestCase's outer transaction wrapper, or
    any other atomic block) poisons that transaction and breaks the
    very-next query. The savepoint contains the rollback to just this
    insert.
    """
    try:
        with transaction.atomic():
            return PromoRedemption.objects.create(
                campaign=campaign,
                user=user,
                outcome=outcome,
                new_trial_ends_at=new_trial_ends_at,
            )
    except IntegrityError:
        return None
