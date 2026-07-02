"""Render one of the privacy-rotation campaign emails with sample
context and send a single copy to a chosen address.

Lets the operator eyeball the actual rendered HTML in their real
inbox before pulling the trigger fleet-wide.

Usage::

    python manage.py preview_email --kind 1 --to mj@bywayofmj.com
    python manage.py preview_email --kind 2 --to mj@bywayofmj.com
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string


class Command(BaseCommand):
    help = "Send a rendered preview of email 1 (reset) or 2 (promo) to a given address."

    def add_arguments(self, parser):
        # --kind selects one of the built-in templates; --template-base renders
        # an arbitrary promo-style template (e.g. the comeback campaign). Exactly
        # one of the two must be given.
        parser.add_argument("--kind", type=int, choices=[1, 2], help="Built-in template: 1=reset, 2=promo.")
        parser.add_argument(
            "--template-base",
            help=(
                "Render an arbitrary promo-style template by path prefix "
                "(e.g. 'email/comeback_2026_07/email'). Renders "
                "<base>_subject.txt, <base>_body.html, <base>_body.txt with the "
                "full promo context (promo_url, valid_until, unsubscribe_url). "
                "Mutually exclusive with --kind."
            ),
        )
        parser.add_argument("--to", required=True, help="Recipient email address.")
        parser.add_argument(
            "--display-name",
            default="Preview Recipient",
            help="Sample display_name to render into the template.",
        )

    def handle(self, *args, kind: int | None, template_base: str | None, to: str, display_name: str, **opts):
        if bool(kind) == bool(template_base):
            raise CommandError("Pass exactly one of --kind or --template-base.")

        if template_base:
            template_root = template_base
            ctx = self._promo_ctx(display_name)
        elif kind == 1:
            template_root = "email/privacy_rotation_2026/email_1"
            ctx = {
                "display_name": display_name,
                "reset_url": "https://neighborhoodunited.org/reset-password?uid=PREVIEW&token=PREVIEW",
            }
        elif kind == 2:
            template_root = "email/privacy_rotation_2026/email_2"
            ctx = self._promo_ctx(display_name)
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
        label = template_base or f"kind={kind}"
        self.stdout.write(self.style.SUCCESS(f"Sent {label} preview to {to}"))

    @staticmethod
    def _promo_ctx(display_name: str) -> dict:
        """Sample context for any promo-style template (kind 2 + --template-base).

        Sample ``valid_until`` so the "Good through …" line renders, and a
        placeholder ``unsubscribe_url`` so the List-Unsubscribe footer added to
        the comeback template isn't blank in the preview.
        """
        return {
            "display_name": display_name,
            # On-brand link (bounces through the live redeem flow).
            "promo_url": "https://neighborhoodunited.org/promo/redeem?code=preview&token=PREVIEW",
            "unsubscribe_url": "https://neighborhoodunited.org/api/v1/tenants/unsubscribe/PREVIEW/",
            "valid_until": datetime(2026, 7, 20, tzinfo=UTC),
        }
