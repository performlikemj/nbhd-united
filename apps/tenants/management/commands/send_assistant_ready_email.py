"""Email specific users that their assistant is now provisioned and ready.

One-off, post-incident courtesy note for users whose tenant failed to
provision at signup (the web-signup handoff / `:latest` outage) and has since
been fixed. Reuses the Privacy-Rotation Email 2 visual shell via the
``email/assistant_ready_2026/`` templates and the same ``send_mail`` path as
``send_promo_campaign`` (live prod Mailgun config + DEFAULT_FROM_EMAIL).

Safety: refuses to email a user whose tenant is not ACTIVE (so we never tell
someone "your assistant is ready" before it actually is) — pass ``--force`` to
override.

Usage::

    python manage.py send_assistant_ready_email \\
        --email jacksonroaster@gmail.com \\
        --email ethaneast2022@gmail.com \\
        --email nak2002k@gmail.com \\
        --email hello@chadaebowler.com

    python manage.py send_assistant_ready_email --email a@b.com --dry-run
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string

from apps.tenants.models import Tenant, User

logger = logging.getLogger(__name__)

# Names that are clearly placeholders, not something to greet a person by.
_JUNK_NAMES = {"", "test", "there", "friend", "user", "hey", "na", "n/a", "none"}


def _clean_display_name(raw: str | None) -> str:
    name = (raw or "").strip()
    return "there" if name.lower() in _JUNK_NAMES else name


class Command(BaseCommand):
    help = "Email specific users that their assistant is provisioned and ready (idempotent send)."

    def add_arguments(self, parser):
        parser.add_argument("--email", action="append", default=[], help="Recipient email (repeatable).")
        parser.add_argument(
            "--app-url",
            default="https://hoodunited.org",
            help="Where the CTA opens NBHD (default https://hoodunited.org).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even to users whose tenant is not ACTIVE (default: skip them).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Render + print who would be emailed without sending.",
        )

    def handle(self, *args, email, app_url, force, dry_run, **opts):
        if not email:
            raise CommandError("Provide at least one --email")

        sent = 0
        skipped = 0
        failed = 0

        for address in email:
            try:
                user = User.objects.select_related("tenant").get(email=address)
            except User.DoesNotExist:
                raise CommandError(f"No user with email {address}") from None

            tenant = getattr(user, "tenant", None)
            tenant_status = getattr(tenant, "status", None)
            active = tenant is not None and tenant_status == Tenant.Status.ACTIVE
            if not active and not force:
                self.stdout.write(
                    self.style.WARNING(
                        f"SKIP {address} — tenant status={tenant_status or 'none'} (not ACTIVE). "
                        f"Provision first, or pass --force."
                    )
                )
                skipped += 1
                continue

            context = {
                "display_name": _clean_display_name(getattr(user, "display_name", None)),
                "app_url": app_url,
            }
            subject = render_to_string("email/assistant_ready_2026/subject.txt", context).strip()
            text_body = render_to_string("email/assistant_ready_2026/body.txt", context)
            html_body = render_to_string("email/assistant_ready_2026/body.html", context)

            if dry_run:
                self.stdout.write(
                    f'[dry-run] would email {address} as "{context["display_name"]}" '
                    f"(tenant={tenant_status}) — subject: {subject!r}"
                )
                continue

            try:
                send_mail(
                    subject=subject,
                    message=text_body,
                    from_email=None,  # DEFAULT_FROM_EMAIL
                    recipient_list=[address],
                    html_message=html_body,
                    fail_silently=False,
                )
                sent += 1
                self.stdout.write(self.style.SUCCESS(f"sent → {address}"))
            except Exception:
                failed += 1
                logger.exception("send_assistant_ready_email: failed for %s", address)
                self.stdout.write(self.style.ERROR(f"FAILED → {address}"))

        self.stdout.write(self.style.SUCCESS("=" * 50))
        self.stdout.write(self.style.SUCCESS(f"sent={sent} skipped={skipped} failed={failed} dry_run={dry_run}"))
        if settings.DEBUG:
            self.stdout.write("(DEBUG on — emails went to the console backend, not real inboxes.)")
