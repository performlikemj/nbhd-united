"""Promo redemption HTTP surface.

``GET /api/v1/promos/redeem/?code=<campaign>&token=<signed>`` verifies
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

    # Defensive: a paying subscriber shouldn't have been emailed in the
    # first place (audience filter excludes them), but if they end up
    # here, don't perturb their billing state by setting is_trial=True.
    if tenant.stripe_subscription_id:
        _record_redemption(campaign, user, PromoRedemption.Outcome.ALREADY_SUBSCRIBED, new_trial_ends_at=None)
        return _redirect("active_subscription")

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
            tenant.save(update_fields=["trial_ends_at", "is_trial", "status", "updated_at"])
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
