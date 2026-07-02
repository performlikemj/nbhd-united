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
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils.dateparse import parse_datetime
from django.utils.http import urlencode

from apps.tenants.models import Tenant, User
from apps.tenants.promo_models import PromoCampaign
from apps.tenants.promo_signing import make_promo_token
from apps.tenants.unsubscribe_signing import make_unsubscribe_token

logger = logging.getLogger(__name__)

# Audience modes selectable via --audience.
AUDIENCE_DEFAULT = "default"
AUDIENCE_COMEBACK = "comeback"


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
            "--template-base",
            default="email/privacy_rotation_2026/email_2",
            help=(
                "Template path prefix for this campaign's email. The command "
                "renders <base>_subject.txt, <base>_body.html and <base>_body.txt. "
                "Default is the original privacy-rotation email; pass e.g. "
                "'email/ios_relaunch_2026/email' for the iOS relaunch send."
            ),
        )
        parser.add_argument(
            "--audience",
            default=AUDIENCE_DEFAULT,
            choices=[AUDIENCE_DEFAULT, AUDIENCE_COMEBACK],
            help=(
                "Audience selector. 'default' (unchanged): active-trial + "
                "suspended-never-subscribed. 'comeback': every onboarded, "
                "has-messaged tenant that is ACTIVE or SUSPENDED — deliberately "
                "includes paid-then-lapsed (SUSPENDED with a retained "
                "stripe_subscription_id) and excludes never-onboarded / "
                "never-messaged shells. Opted-out users are excluded from both."
            ),
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
        template_base: str,
        audience: str,
        dry_run: bool,
        **opts,
    ):
        valid_until_dt = parse_datetime(valid_until)
        if valid_until_dt is None:
            raise CommandError(f"Invalid --valid-until: {valid_until!r}")

        owner_email = (getattr(settings, "PLATFORM_OWNER_EMAIL", "") or "").strip().lower()
        audience_qs = self._build_audience_qs(audience, owner_email)

        audience_users = list(audience_qs)
        self.stdout.write(f"Audience[{audience}]: {len(audience_users)} user(s) (excluding owner + opted-out)")

        if dry_run:
            for user in audience_users:
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
                    "user_ids": [str(u.id) for u in audience_users],
                    "captured_at_count": len(audience_users),
                    "audience_mode": audience,
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

        for user in audience_users:
            token = make_promo_token(campaign.code, user.id)
            qs = urlencode({"code": campaign.code, "token": token})
            promo_url = f"{frontend_url}/promo/redeem?{qs}"
            unsubscribe_url = self._unsubscribe_url(user)

            try:
                self._send_promo_email(
                    user,
                    promo_url=promo_url,
                    unsubscribe_url=unsubscribe_url,
                    valid_until=campaign.valid_until,
                    template_base=template_base,
                )
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

    def _build_audience_qs(self, audience: str, owner_email: str):
        """Return the User queryset for the requested audience mode.

        Both modes require a tenant, a non-empty email, and exclude the
        platform owner and anyone who has opted out of marketing email
        (``User.email_opt_out``).

        - ``default`` (unchanged): active trial (ACTIVE + is_trial) OR
          suspended-never-subscribed (SUSPENDED + empty
          stripe_subscription_id). Excludes paying subscribers.
        - ``comeback``: every onboarded, has-messaged tenant that is
          ACTIVE or SUSPENDED. Deliberately *includes* paid-then-lapsed
          tenants (SUSPENDED with a retained stripe_subscription_id) that
          the default filter drops, and *excludes* never-onboarded /
          never-messaged shells.
        """
        base = User.objects.filter(tenant__isnull=False).exclude(email="").exclude(email_opt_out=True)

        if audience == AUDIENCE_COMEBACK:
            audience_qs = base.filter(
                tenant__onboarding_complete=True,
                tenant__last_message_at__isnull=False,
                tenant__status__in=[Tenant.Status.ACTIVE, Tenant.Status.SUSPENDED],
            )
        else:
            audience_qs = base.filter(
                Q(tenant__status=Tenant.Status.ACTIVE, tenant__is_trial=True)
                | Q(tenant__status=Tenant.Status.SUSPENDED, tenant__stripe_subscription_id="")
            )

        if owner_email:
            audience_qs = audience_qs.exclude(email__iexact=owner_email)

        return audience_qs.select_related("tenant")

    def _unsubscribe_url(self, user: User) -> str:
        """Build the per-user one-click unsubscribe URL.

        Points at the backend Django view (``API_BASE_URL``), not the
        static frontend — the view renders its own confirmation page and
        is the RFC 8058 ``List-Unsubscribe-Post`` target.
        """
        api_base = (getattr(settings, "API_BASE_URL", "") or "http://localhost:8000").rstrip("/")
        token = make_unsubscribe_token(user.id)
        return f"{api_base}/api/v1/tenants/unsubscribe/{token}/"

    def _send_promo_email(
        self,
        user: User,
        *,
        promo_url: str,
        unsubscribe_url: str,
        valid_until=None,
        template_base: str,
    ) -> None:
        context = {
            "display_name": getattr(user, "display_name", None) or "there",
            "promo_url": promo_url,
            "unsubscribe_url": unsubscribe_url,
            # Render the redemption deadline from the campaign row so the copy
            # can never drift from the actual expiry (the original templates
            # hardcoded "June 6, 2026" and went stale).
            "valid_until": valid_until,
        }
        subject = render_to_string(f"{template_base}_subject.txt", context).strip()
        text_body = render_to_string(f"{template_base}_body.txt", context)
        html_body = render_to_string(f"{template_base}_body.html", context)

        # EmailMultiAlternatives (not send_mail) so we can attach the
        # RFC 2369 / RFC 8058 list-unsubscribe headers. Gmail / Yahoo show a
        # native "Unsubscribe" affordance for these and weigh their presence
        # for deliverability — the prior blast had no unsubscribe path and
        # landed 0 redemptions (spam suspected).
        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=None,
            to=[user.email],
            headers={
                "List-Unsubscribe": f"<{unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
