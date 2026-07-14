"""One-off sunset broadcast for the Telegram/LINE channel decommission.

Phase 0.5 of the channel-decommission campaign
(``CONTINUITY_channel_decommission.md``). Notifies every real (non-synthetic)
tenant whose account is linked to Telegram or LINE that NBHD has moved to an
iOS app and that their chat channel is being retired on the cutover date. The
notice goes out over EVERY linked channel (Telegram + LINE) plus a Mailgun
email to the account address, so a user who never opens one still hears it on
another.

This is the ONLY new Telegram/LINE-sending code the campaign adds; it is
retired with Phase 3. It is transactional in intent — these users lose their
current way to reach their assistant, so the notice is sent regardless of
marketing opt-out — and it makes no external calls inside a transaction.

Copy lives in module-level constants below so it can be reviewed and tweaked
in the PR. Substance is MJ-approved (2026-07-14): NBHD is now an iOS app;
two more weeks of continued use "to see what NBHD can do as their personal
assistant"; the explicit cutover date; the App Store link.

Usage::

    # dry run (DEFAULT) — prints target count + tenant ids only, sends nothing
    python manage.py send_channel_sunset_notice

    # actually send (cutover date REQUIRED with --execute)
    python manage.py send_channel_sunset_notice --execute --cutover-date 2026-07-28

    # scope to specific tenants (repeatable)
    python manage.py send_channel_sunset_notice --execute --cutover-date 2026-07-28 \\
        --tenant-id <uuid> --tenant-id <uuid>
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.tenants.models import User

logger = logging.getLogger(__name__)

# Canonical App Store URL — kept in sync with the frontend badge
# (``frontend/components/app-store-badge.tsx``: APP_STORE_URL). A test asserts
# they match so this can't silently drift.
APP_STORE_URL = "https://apps.apple.com/us/app/nbhd/id6779158519"

# ---------------------------------------------------------------------------
# Message copy — edit freely in review. Substance MJ-approved 2026-07-14.
# ``{cutover_date}`` renders as e.g. "July 28, 2026"; ``{app_store_url}`` and
# ``{display_name}`` are substituted at send time. No other literal braces.
# ---------------------------------------------------------------------------

# Short plain text for the live channel (Telegram / LINE).
CHANNEL_MESSAGE = (
    "Hi from NBHD 👋\n\n"
    "We've moved NBHD into an iOS app, and this chat channel is being retired. "
    "Your assistant will keep answering you here until {cutover_date} — about "
    "two more weeks — so you have time to see what NBHD can do as your personal "
    "assistant before the switch.\n\n"
    "Download the app and pick up right where you left off:\n"
    "{app_store_url}\n\n"
    "Thanks for being part of this.\n"
    "— NBHD"
)

EMAIL_SUBJECT = "NBHD is now an app — and your chat channel is winding down"

# Same substance as the channel message, a little fuller for email.
EMAIL_BODY = (
    "Hi {display_name},\n\n"
    "A short but important note about your NBHD assistant.\n\n"
    "NBHD has become an iOS app. The same private assistant you've been "
    "messaging is now a tap away on your iPhone home screen.\n\n"
    "Because of that move, we're retiring the chat channel you use today. Your "
    "assistant will keep replying there until {cutover_date} — about two more "
    "weeks. We wanted to give you that window on purpose: enough time to "
    "download the app and really see what NBHD can do as your personal "
    "assistant before anything changes.\n\n"
    "Download NBHD on the App Store:\n"
    "{app_store_url}\n\n"
    "After {cutover_date}, the app becomes the way to reach your assistant. "
    "Everything you've built with it comes along — nothing is lost.\n\n"
    "Questions? Just reply to this email.\n\n"
    "Thanks for being here,\n"
    "— The NBHD team\n"
    "neighborhoodunited.org"
)


def format_cutover_date(cutover_date: str) -> str:
    """Render ``YYYY-MM-DD`` as ``"July 28, 2026"`` (no platform-specific
    ``%-d`` so it works on macOS and Linux alike). Raises ``ValueError`` on a
    malformed date."""
    dt = datetime.strptime(cutover_date, "%Y-%m-%d")
    return f"{dt:%B} {dt.day}, {dt.year}"


def render_channel_message(cutover_display: str) -> str:
    return CHANNEL_MESSAGE.format(cutover_date=cutover_display, app_store_url=APP_STORE_URL)


def render_email(display_name: str, cutover_display: str) -> tuple[str, str]:
    body = EMAIL_BODY.format(
        display_name=display_name or "there",
        cutover_date=cutover_display,
        app_store_url=APP_STORE_URL,
    )
    return EMAIL_SUBJECT, body


def build_target_queryset(tenant_ids: list[str] | None = None):
    """Real (non-synthetic) tenants whose user is linked to Telegram or LINE.

    The synthetic filter automatically excludes the App Review demo tenant and
    every eval synthetic — no marketing/service mail is issued against them.
    A user linked to both channels matches once (Q-OR dedupes).
    """
    linked = Q(telegram_chat_id__isnull=False) | (Q(line_user_id__isnull=False) & ~Q(line_user_id=""))
    qs = User.objects.filter(tenant__is_synthetic=False).filter(linked).select_related("tenant")
    if tenant_ids:
        qs = qs.filter(tenant__id__in=tenant_ids)
    return qs.order_by("tenant__id")


# ---------------------------------------------------------------------------
# Per-channel senders (module-level so tests can patch/exercise them
# independently). Each returns True on a confirmed send, False otherwise, and
# never raises — the caller's per-tenant try/except is a backstop, not the
# primary guard.
# ---------------------------------------------------------------------------


def send_telegram_notice(chat_id: int, text: str) -> bool:
    # Local import so ``patch("apps.router.services.send_telegram_message")``
    # intercepts at call time (see docs/agents/backend.md).
    from apps.router.services import send_telegram_message

    return send_telegram_message(chat_id, text)


def send_line_notice(tenant, line_user_id: str, text: str) -> bool:
    """Plain-text LINE Push with the established quota bookkeeping — mirrors
    ``apps/core/services._send_line_text`` (quota tripwire + outbound record)
    rather than hand-rolling a bare POST."""
    if not line_user_id:
        return False
    access_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "")
    if not access_token:
        logger.warning("sunset: LINE_CHANNEL_ACCESS_TOKEN not configured")
        return False

    messages = [{"type": "text", "text": text[:4900]}]
    try:
        resp = httpx.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"to": line_user_id, "messages": messages},
            timeout=10,
        )
    except Exception:
        logger.exception("sunset: LINE push error")
        return False

    if not resp.is_success:
        logger.warning("sunset: LINE push failed (%s): %s", resp.status_code, resp.text[:200])
        # Trip the fleet-wide quota gate if this is the monthly-cap 429.
        from apps.router.line_webhook import _maybe_trip_monthly_quota

        _maybe_trip_monthly_quota(resp.status_code, resp.text)
        return False

    # Record sent message ids so a user quote-reply attributes correctly.
    try:
        from apps.router.line_webhook import _record_line_outbound

        sent = (resp.json() or {}).get("sentMessages") or []
        _record_line_outbound(tenant, line_user_id, sent, messages)
    except Exception:
        logger.debug("sunset: LINE outbound record failed", exc_info=True)
    return True


def send_email_notice(user, subject: str, body: str) -> bool:
    """Send the sunset email to ``user.email`` via the configured backend
    (Mailgun SMTP in prod). ``from_email=None`` uses DEFAULT_FROM_EMAIL — no
    hardcoded addresses. Returns False (no raise) when there is no recipient."""
    recipient = (user.email or "").strip()
    if not recipient:
        return False
    send_mail(
        subject=subject,
        message=body,
        from_email=None,  # DEFAULT_FROM_EMAIL
        recipient_list=[recipient],
        fail_silently=False,
    )
    return True


class Command(BaseCommand):
    help = (
        "Broadcast the channel-decommission sunset notice to every real "
        "(non-synthetic) Telegram/LINE-linked tenant over their live "
        "channel(s) + email. Dry-run by default; --execute to send."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually send. Without this the command is a dry run (prints targets only).",
        )
        parser.add_argument(
            "--cutover-date",
            dest="cutover_date",
            default=None,
            help="YYYY-MM-DD the channel is retired. REQUIRED with --execute; rendered into the copy.",
        )
        parser.add_argument(
            "--tenant-id",
            dest="tenant_ids",
            action="append",
            default=None,
            help="Scope to this tenant id (repeatable). Omit to target all channel-linked real tenants.",
        )

    def handle(self, *args, execute: bool, cutover_date: str | None, tenant_ids: list[str] | None, **opts):
        users = list(build_target_queryset(tenant_ids))

        tg_targets = sum(1 for u in users if u.telegram_chat_id)
        line_targets = sum(1 for u in users if (u.line_user_id or "").strip())
        email_targets = sum(1 for u in users if (u.email or "").strip())

        self.stdout.write(f"Targets: {len(users)} tenant(s)")
        self.stdout.write(f"Channels: telegram={tg_targets}, line={line_targets}, email={email_targets}")

        if not execute:
            self.stdout.write(self.style.WARNING("[dry-run] no messages will be sent. Tenant ids:"))
            for u in users:
                # Tenant id only — no email/chat id in dry-run output.
                self.stdout.write(f"  {u.tenant.id}")
            self.stdout.write("Run with --execute --cutover-date YYYY-MM-DD to send.")
            return

        if not cutover_date:
            raise CommandError("--cutover-date YYYY-MM-DD is required with --execute.")
        try:
            cutover_display = format_cutover_date(cutover_date)
        except ValueError:
            raise CommandError(f"Invalid --cutover-date {cutover_date!r}; expected YYYY-MM-DD.")

        channel_text = render_channel_message(cutover_display)

        stats = {
            "tg_sent": 0,
            "tg_failed": 0,
            "line_sent": 0,
            "line_failed": 0,
            "email_sent": 0,
            "email_failed": 0,
            "tenant_errors": 0,
        }

        for user in users:
            # Per-tenant isolation: one tenant's failure never halts the run.
            try:
                if user.telegram_chat_id:
                    if send_telegram_notice(user.telegram_chat_id, channel_text):
                        stats["tg_sent"] += 1
                    else:
                        stats["tg_failed"] += 1

                line_uid = (user.line_user_id or "").strip()
                if line_uid:
                    if send_line_notice(user.tenant, line_uid, channel_text):
                        stats["line_sent"] += 1
                    else:
                        stats["line_failed"] += 1

                if (user.email or "").strip():
                    subject, body = render_email(user.display_name, cutover_display)
                    try:
                        if send_email_notice(user, subject, body):
                            stats["email_sent"] += 1
                        else:
                            stats["email_failed"] += 1
                    except Exception:
                        stats["email_failed"] += 1
                        logger.exception("sunset: email failed for tenant %s", user.tenant.id)
            except Exception:
                stats["tenant_errors"] += 1
                logger.exception("sunset: broadcast failed for tenant %s", getattr(user, "tenant", None))

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS(f"Cutover date rendered as: {cutover_display}"))
        self.stdout.write(f"Telegram: sent={stats['tg_sent']} failed={stats['tg_failed']}")
        self.stdout.write(f"LINE:     sent={stats['line_sent']} failed={stats['line_failed']}")
        self.stdout.write(f"Email:    sent={stats['email_sent']} failed={stats['email_failed']}")
        if stats["tenant_errors"]:
            self.stdout.write(self.style.ERROR(f"Tenant-level errors: {stats['tenant_errors']}"))
        self.stdout.write(self.style.SUCCESS(f"Tenants processed: {len(users)}"))
