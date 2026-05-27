"""Render one of the privacy-rotation campaign emails with sample
context and send a single copy to a chosen address.

Lets the operator eyeball the actual rendered HTML in their real
inbox before pulling the trigger fleet-wide.

Usage::

    python manage.py preview_email --kind 1 --to mj@bywayofmj.com
    python manage.py preview_email --kind 2 --to mj@bywayofmj.com
"""

from __future__ import annotations

from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string


class Command(BaseCommand):
    help = "Send a rendered preview of email 1 (reset) or 2 (promo) to a given address."

    def add_arguments(self, parser):
        parser.add_argument("--kind", type=int, choices=[1, 2], required=True)
        parser.add_argument("--to", required=True, help="Recipient email address.")
        parser.add_argument(
            "--display-name",
            default="Preview Recipient",
            help="Sample display_name to render into the template.",
        )

    def handle(self, *args, kind: int, to: str, display_name: str, **opts):
        if kind == 1:
            template_root = "email/privacy_rotation_2026/email_1"
            ctx = {
                "display_name": display_name,
                "reset_url": "https://neighborhoodunited.org/reset-password?uid=PREVIEW&token=PREVIEW",
            }
        elif kind == 2:
            template_root = "email/privacy_rotation_2026/email_2"
            ctx = {
                "display_name": display_name,
                "promo_url": "https://neighborhoodunited.org/promo/redeem?code=preview&token=PREVIEW",
            }
        else:
            raise CommandError(f"Unsupported --kind: {kind}")

        subject = render_to_string(f"{template_root}_subject.txt", ctx).strip()
        text_body = render_to_string(f"{template_root}_body.txt", ctx)
        html_body = render_to_string(f"{template_root}_body.html", ctx)

        send_mail(
            subject=f"[PREVIEW] {subject}",
            message=text_body,
            from_email=None,
            recipient_list=[to],
            html_message=html_body,
            fail_silently=False,
        )
        self.stdout.write(self.style.SUCCESS(f"Sent kind={kind} preview to {to}"))
