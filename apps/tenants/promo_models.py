"""Promotional campaign models.

Models for fleet-wide promotional events (trial extensions, discount
codes, etc.). Each campaign has a unique code and a deadline; each
redemption is one row per (campaign, user), with a unique-together
constraint that makes double-click idempotency a natural DB invariant
instead of an application-layer race-condition risk.

The HMAC signing of per-user redemption tokens lives in
``apps/tenants/promo_signing.py``; the redemption view lives in
``apps/tenants/promo_views.py``; the audience-filtering + email-send
fan-out lives in the ``send_promo_campaign`` management command.
"""

import uuid

from django.conf import settings
from django.db import models


class PromoCampaign(models.Model):
    """A single promotional event — e.g. the June 2026 privacy-rotation
    trial extension. One row per campaign. The ``code`` lives in
    redemption URLs so the view can locate the campaign + check its
    deadline without trusting any data carried in the user token.
    """

    class Kind(models.TextChoices):
        TRIAL_EXTENSION = "trial_extension", "Trial extension"
        # Future: STRIPE_COUPON, FEATURE_UNLOCK, etc.

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(
        max_length=64,
        unique=True,
        help_text="URL-safe campaign identifier (e.g. 'privacy-june-2026').",
    )
    kind = models.CharField(
        max_length=32,
        choices=Kind.choices,
        default=Kind.TRIAL_EXTENSION,
    )
    extension_days = models.PositiveSmallIntegerField(
        default=0,
        help_text="Days to add to trial_ends_at when redeemed (for TRIAL_EXTENSION kind).",
    )
    valid_until = models.DateTimeField(
        help_text="Hard deadline — redemption rejected after this timestamp.",
    )
    audience_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Frozen audit of which user IDs the campaign targeted. Written "
            "at send time so we can compare against redemptions later."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "promo_campaigns"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"PromoCampaign({self.code}, {self.kind})"


class PromoRedemption(models.Model):
    """One row per (campaign, user) on first redemption attempt.

    The ``unique_together`` constraint is load-bearing: a second click
    on the same per-user token hits the constraint, the view catches
    IntegrityError, and returns the existing row's outcome instead of
    extending the trial a second time. Concurrent double-clicks
    serialize at the DB layer rather than the application layer.
    """

    class Outcome(models.TextChoices):
        EXTENDED = "extended", "Trial extended"
        ALREADY_SUBSCRIBED = "already_subscribed", "User has an active subscription"
        NO_TENANT = "no_tenant", "User has no associated tenant"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(
        PromoCampaign,
        on_delete=models.CASCADE,
        related_name="redemptions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="promo_redemptions",
    )
    redeemed_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(
        max_length=32,
        choices=Outcome.choices,
        default=Outcome.EXTENDED,
    )
    new_trial_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="trial_ends_at value after the extension applied. Null for non-EXTENDED outcomes.",
    )

    class Meta:
        db_table = "promo_redemptions"
        ordering = ["-redeemed_at"]
        unique_together = [("campaign", "user")]
        indexes = [
            models.Index(fields=["user", "-redeemed_at"], name="promo_red_user_idx"),
        ]

    def __str__(self) -> str:
        return f"PromoRedemption({self.campaign.code}, user={self.user_id}, {self.outcome})"
