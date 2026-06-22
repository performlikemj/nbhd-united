"""Force-rotate every user's password and email them a reset link.

Drives the June 2026 privacy-rotation campaign. Iterates all users
*except* the platform owner (matched on ``settings.PLATFORM_OWNER_EMAIL``
so the operator running the command doesn't lock themselves out), sets
``set_unusable_password()`` on each (which bumps
``password_last_changed_at`` via the model override → invalidates every
outstanding JWT for that user), then emails them a reset link.

Idempotent — re-running the command skips users whose
``password_last_changed_at`` is already >= the command's
``--since`` cutoff. So a partial-Mailgun-outage retry only re-emails
users that didn't get the first wave.

If a user's password was rotated but the reset email failed, that user is
locked out (unusable password, no reset link) and a same-``--since`` retry
silently skips them. Use ``--resend-only`` to re-send the reset email to
every already-rotated user (unusable password) without re-rotating — this
recovers email-failed users regardless of ``--since``.

Usage::

    python manage.py rotate_all_passwords --reason="june-2026-privacy-hygiene"
    python manage.py rotate_all_passwords --reason="..." --dry-run
    python manage.py rotate_all_passwords --reason="..." --since 2026-06-01T00:00:00Z
    python manage.py rotate_all_passwords --reason="..." --resend-only
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.tenants.models import User

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Rotate every non-owner user's password, email them a reset link. "
        "Idempotent — re-runs skip users already rotated since --since."
    )

    def add_arguments(self, parser):
        parser.add_argument("--reason", required=True, help="Audit string for log lines.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen without rotating or sending email.",
        )
        parser.add_argument(
            "--since",
            type=str,
            default=None,
            help=(
                "ISO datetime. Users with password_last_changed_at >= this "
                "are skipped (idempotency on retry). Defaults to now() at "
                "command start — so a single run rotates everyone, and a "
                "subsequent run with the same --since picks up users the "
                "first run missed."
            ),
        )
        parser.add_argument(
            "--resend-only",
            action="store_true",
            help=(
                "Do not rotate any passwords. Only re-send the reset email to "
                "users who are already rotated (unusable password) — recovers "
                "users whose reset email failed on a prior run without "
                "re-bumping password_last_changed_at. Ignores --since."
            ),
        )

    def handle(
        self,
        *args,
        reason: str,
        dry_run: bool,
        since: str | None,
        resend_only: bool = False,
        **opts,
    ):
        if resend_only:
            return self._handle_resend_only(reason=reason, dry_run=dry_run)

        owner_email = (getattr(settings, "PLATFORM_OWNER_EMAIL", "") or "").strip().lower()
        if not owner_email:
            self.stdout.write(
                self.style.WARNING(
                    "PLATFORM_OWNER_EMAIL not set — no owner exemption will apply. "
                    "This will lock you out of the dashboard if you don't have your "
                    "email-reset path ready. Set PLATFORM_OWNER_EMAIL in the Container "
                    "App env, or proceed knowing you'll need to reset like everyone else."
                )
            )

        since_dt = self._parse_since(since)
        self.stdout.write(f"Rotating passwords (reason={reason}, dry_run={dry_run}, since={since_dt.isoformat()})")

        qs = User.objects.all()
        if owner_email:
            qs = qs.exclude(email__iexact=owner_email)

        rotated = 0
        skipped_already = 0
        skipped_no_email = 0
        email_failed = 0

        for user in qs.iterator():
            if not user.email:
                skipped_no_email += 1
                continue
            if user.password_last_changed_at and user.password_last_changed_at >= since_dt:
                skipped_already += 1
                continue

            if dry_run:
                self.stdout.write(f"  [dry-run] would rotate {user.email}")
                continue

            # 1. Rotate the password. set_unusable_password bumps
            #    password_last_changed_at via the model override.
            user.set_unusable_password()
            user.save(update_fields=["password", "password_last_changed_at"])

            # 2. Generate a reset token against the new (unusable) hash.
            #    Django's default_token_generator HMACs the hash + last_login,
            #    so the token stays valid until the user sets a new password
            #    (at which point the hash changes and the token expires
            #    naturally).
            try:
                self._send_reset_email(user, reason=reason)
                rotated += 1
            except Exception:
                email_failed += 1
                logger.exception(
                    "rotate_all_passwords: email send failed for user %s (rotated, locked out until manual resend)",
                    user.id,
                )

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS(f"Rotated + emailed (got reset link): {rotated}"))
        self.stdout.write(f"Skipped (already rotated since --since): {skipped_already}")
        self.stdout.write(f"Skipped (no email address):              {skipped_no_email}")
        # Total passwords actually rotated (locked out) = those emailed plus those
        # whose email failed. Surface it as one number so a partial-Mailgun-outage
        # run doesn't undercount the real blast radius.
        self.stdout.write(
            self.style.WARNING(f"Total passwords rotated / locked out: {rotated + email_failed}")
        )
        if email_failed:
            self.stdout.write(
                self.style.ERROR(
                    f"⚠️  Email failed (rotated but no reset link sent): {email_failed} — "
                    f"these users are locked out; re-run with --resend-only to re-send their reset link"
                )
            )

    def _handle_resend_only(self, *, reason: str, dry_run: bool):
        """Re-send the reset email to already-rotated users without touching
        their password. Recovers users whose email failed on a prior run.

        Targets users with an email and an unusable password (the state
        ``set_unusable_password()`` leaves them in). Does NOT re-bump
        ``password_last_changed_at``, so outstanding JWTs stay invalidated
        and the existing reset token remains valid.
        """
        owner_email = (getattr(settings, "PLATFORM_OWNER_EMAIL", "") or "").strip().lower()
        self.stdout.write(f"Resending reset links (reason={reason}, dry_run={dry_run})")

        qs = User.objects.all()
        if owner_email:
            qs = qs.exclude(email__iexact=owner_email)

        resent = 0
        skipped_no_email = 0
        skipped_usable = 0
        email_failed = 0

        for user in qs.iterator():
            if not user.email:
                skipped_no_email += 1
                continue
            if user.has_usable_password():
                # Never rotated (or already reset) — nothing to resend.
                skipped_usable += 1
                continue

            if dry_run:
                self.stdout.write(f"  [dry-run] would resend reset link to {user.email}")
                continue

            try:
                self._send_reset_email(user, reason=reason)
                resent += 1
            except Exception:
                email_failed += 1
                logger.exception(
                    "rotate_all_passwords --resend-only: email send failed for user %s",
                    user.id,
                )

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS(f"Reset links re-sent: {resent}"))
        self.stdout.write(f"Skipped (usable password — not rotated): {skipped_usable}")
        self.stdout.write(f"Skipped (no email address):              {skipped_no_email}")
        if email_failed:
            self.stdout.write(
                self.style.ERROR(
                    f"⚠️  Resend still failed for {email_failed} user(s) — "
                    f"check logs and retry --resend-only"
                )
            )

    def _parse_since(self, since: str | None) -> datetime:
        if since:
            dt = parse_datetime(since)
            if dt is None:
                raise CommandError(f"Invalid --since: {since!r} (expected ISO 8601)")
            if dt.tzinfo is None:
                dt = timezone.make_aware(dt, timezone.utc)
            return dt
        return timezone.now()

    def _send_reset_email(self, user: User, *, reason: str) -> None:
        frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        reset_url = f"{frontend_url}/reset-password?uid={uid}&token={token}"

        context = {
            "display_name": getattr(user, "display_name", None) or "there",
            "reset_url": reset_url,
        }
        subject = render_to_string(
            "email/privacy_rotation_2026/email_1_subject.txt",
            context,
        ).strip()
        text_body = render_to_string(
            "email/privacy_rotation_2026/email_1_body.txt",
            context,
        )
        html_body = render_to_string(
            "email/privacy_rotation_2026/email_1_body.html",
            context,
        )

        send_mail(
            subject=subject,
            message=text_body,
            from_email=None,  # uses DEFAULT_FROM_EMAIL
            recipient_list=[user.email],
            html_message=html_body,
            fail_silently=False,
        )
        logger.info(
            "rotate_all_passwords: rotated + emailed user %s (reason=%s)",
            user.id,
            reason,
        )
