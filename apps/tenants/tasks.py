"""Tasks for tenant maintenance (executed via QStash)."""

from django.utils import timezone

from .services import reset_daily_counters, reset_monthly_counters
from .telegram_models import TelegramLinkToken


def reset_daily_counters_task():
    """Reset daily message counters. Schedule: daily at midnight UTC."""
    return reset_daily_counters()


def reset_monthly_counters_task():
    """Reset monthly counters. Schedule: 1st of month at midnight UTC."""
    return reset_monthly_counters()


def cleanup_expired_telegram_tokens():
    """Purge expired TelegramLinkTokens older than 1 hour."""
    cutoff = timezone.now() - timezone.timedelta(hours=1)
    deleted, _ = TelegramLinkToken.objects.filter(expires_at__lt=cutoff).delete()
    return f"Deleted {deleted} expired tokens"


def rotate_all_passwords_task() -> dict:
    """QStash-dispatched wrapper around the ``rotate_all_passwords``
    management command. Used for the scheduled June 1 fire — registered
    in apps/cron/views.py TASK_MAP and triggered by a one-off QStash
    message that's set via the upstash MCP before the campaign date.

    Defaults to the privacy-hygiene reason; pulls all settings from the
    campaign date (today). For ad-hoc rotations, use the management
    command directly with custom flags.
    """
    from io import StringIO

    from django.core.management import call_command

    buf = StringIO()
    call_command(
        "rotate_all_passwords",
        reason="june-2026-privacy-hygiene",
        stdout=buf,
    )
    return {"output": buf.getvalue()[-2000:]}  # tail of stdout for audit


def send_promo_campaign_task() -> dict:
    """QStash-dispatched wrapper around ``send_promo_campaign`` for the
    June 2 trial-extension blast.

    Constants are inlined here on purpose — this task fires exactly once
    on a known date. The management command remains available for
    ad-hoc / future campaigns with different parameters.
    """
    from io import StringIO

    from django.core.management import call_command

    buf = StringIO()
    call_command(
        "send_promo_campaign",
        code="privacy-june-2026",
        kind="trial_extension",
        days=14,
        valid_until="2026-06-06T00:00:00+00:00",
        stdout=buf,
    )
    return {"output": buf.getvalue()[-2000:]}
