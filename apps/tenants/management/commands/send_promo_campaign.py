"""Send a promotional-campaign email to a filtered audience.

Creates a :class:`PromoCampaign` row, snapshots the audience, and
emails each eligible user a per-user HMAC-signed redemption link. The
``redeem_promo`` view in ``apps/tenants/promo_views.py`` verifies the
signature and applies the trial extension when a user clicks.

Idempotent: the campaign code is unique, so re-running with the same
``--code`` reuses the existing row (audience snapshot is **not**
overwritten on re-run — first run wins, so a partial-failure retry
won't accidentally widen the audience). Emails are sent to every
audience row each invocation, but the redemption view's
``unique_together(campaign, user)`` means a click on either email
extends the trial once.

Usage::

    python manage.py send_promo_campaign \\
        --code privacy-june-2026 \\
        --kind trial_extension \\
        --days 14 \\
        --valid-until 2026-06-06T00:00:00Z

    python manage.py send_promo_campaign --code ... --days 14 \\
        --valid-until 2026-06-06T00:00:00Z --dry-run
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils.dateparse import parse_datetime
from django.utils.http import urlencode

from apps.tenants.models import Tenant, User
from apps.tenants.promo_models import PromoCampaign
from apps.tenants.promo_signing import make_promo_token

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Create a PromoCampaign and email per-user redemption links to "
        "the eligible audience (active trial + suspended trial-expired-"
        "never-subscribed). Idempotent on --code."
    )

    def add_arguments(self, parser):
        parser.add_argument("--code", required=True, help="URL-safe campaign identifier.")
        parser.add_argument(
            "--kind",
            default="trial_extension",
            choices=[c.value for c in PromoCampaign.Kind],
        )
        parser.add_argument(
            "--days",
            type=int,
            required=True,
            help="Days to extend trial_ends_at when redeemed.",
        )
        parser.add_argument(
            "--valid-until",
            required=True,
            help="ISO datetime — hard deadline; redemption rejected after.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print audience + would-send addresses without creating the campaign or sending.",
        )

    def handle(
        self,
        *args,
        code: str,
        kind: str,
        days: int,
        valid_until: str,
        dry_run: bool,
        **opts,
    ):
        valid_until_dt = parse_datetime(valid_until)
        if valid_until_dt is None:
            raise CommandError(f"Invalid --valid-until: {valid_until!r}")

        # Audience filter:
        #   - active trial: status=ACTIVE AND is_trial=True
        #   - suspended trial-expired-never-subscribed:
        #       status=SUSPENDED AND stripe_subscription_id=""
        # Excludes paying subscribers, never-onboarded (no tenant),
        # deleted, and the platform owner.
        owner_email = (getattr(settings, "PLATFORM_OWNER_EMAIL", "") or "").strip().lower()
        audience_qs = (
            User.objects.filter(tenant__isnull=False)
            .filter(
                Q(tenant__status=Tenant.Status.ACTIVE, tenant__is_trial=True)
                | Q(tenant__status=Tenant.Status.SUSPENDED, tenant__stripe_subscription_id="")
            )
            .exclude(email="")
            .select_related("tenant")
        )
        if owner_email:
            audience_qs = audience_qs.exclude(email__iexact=owner_email)

        audience = list(audience_qs)
        self.stdout.write(
            f"Audience: {len(audience)} user(s) (active trial + suspended-never-subscribed, excluding owner)"
        )

        if dry_run:
            for user in audience:
                self.stdout.write(
                    f"  [dry-run] would email {user.email} "
                    f"(tenant.status={user.tenant.status}, is_trial={user.tenant.is_trial})"
                )
            return

        # Create or reuse the campaign row. First run wins the audience
        # snapshot; subsequent reruns don't widen it even if more users
        # have entered the eligible state.
        campaign, created = PromoCampaign.objects.get_or_create(
            code=code,
            defaults={
                "kind": kind,
                "extension_days": days,
                "valid_until": valid_until_dt,
                "audience_snapshot": {
                    "user_ids": [str(u.id) for u in audience],
                    "captured_at_count": len(audience),
                },
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created PromoCampaign({code})"))
        else:
            self.stdout.write(f"Reusing PromoCampaign({code}) created at {campaign.created_at.isoformat()}")

        sent = 0
        email_failed = 0
        frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")

        for user in audience:
            token = make_promo_token(campaign.code, user.id)
            qs = urlencode({"code": campaign.code, "token": token})
            promo_url = f"{frontend_url}/promo/redeem?{qs}"

            try:
                self._send_promo_email(user, promo_url=promo_url)
                sent += 1
            except Exception:
                email_failed += 1
                logger.exception(
                    "send_promo_campaign: email failed for user %s (campaign=%s)",
                    user.id,
                    campaign.code,
                )

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS(f"Sent: {sent}"))
        if email_failed:
            self.stdout.write(self.style.ERROR(f"Email failed: {email_failed}"))

    def _send_promo_email(self, user: User, *, promo_url: str) -> None:
        context = {
            "display_name": getattr(user, "display_name", None) or "there",
            "promo_url": promo_url,
        }
        subject = render_to_string(
            "email/privacy_rotation_2026/email_2_subject.txt",
            context,
        ).strip()
        text_body = render_to_string(
            "email/privacy_rotation_2026/email_2_body.txt",
            context,
        )
        html_body = render_to_string(
            "email/privacy_rotation_2026/email_2_body.html",
            context,
        )
        send_mail(
            subject=subject,
            message=text_body,
            from_email=None,
            recipient_list=[user.email],
            html_message=html_body,
            fail_silently=False,
        )
