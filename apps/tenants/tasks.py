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


def preview_email_task(kind: int = 1, to: str = "", display_name: str = "Preview") -> dict:
    """QStash-dispatched wrapper around the ``preview_email`` command.

    Lets the operator fire a render-and-send from outside the box
    (e.g. via the upstash MCP) without needing TTY access or
    ``containerapp exec``. Picks up the live Mailgun config the same
    way every other Django send does, so the rendered output mirrors
    what real recipients will see.

    Args (delivered via QStash body kwargs):
      kind: 1 for the password-reset email, 2 for the promo email
      to: recipient email address
      display_name: sample display name to render into the template
    """
    from io import StringIO

    from django.core.management import call_command

    if kind not in (1, 2):
        raise ValueError(f"preview_email_task: invalid kind={kind!r}")
    if not to:
        raise ValueError("preview_email_task: 'to' is required")

    buf = StringIO()
    call_command("preview_email", kind=kind, to=to, display_name=display_name, stdout=buf)
    return {"output": buf.getvalue()}


def send_promo_campaign_task() -> dict:
    """QStash-dispatched wrapper around ``send_promo_campaign`` — the privacy /
    zero-data-retention trial-extension blast (14 days free).

    Constants are inlined here on purpose — this task fires exactly once
    on a known date. The management command remains available for
    ad-hoc / future campaigns with different parameters.

    NOTE: the original June-2026 fire was never triggered (no PromoCampaign row
    was ever created); this is the re-send with a fresh code + redemption window
    and the ZDR messaging. The ``code`` is unique, so the original
    ``privacy-june-2026`` audience snapshot is untouched.
    """
    from io import StringIO

    from django.core.management import call_command

    buf = StringIO()
    call_command(
        "send_promo_campaign",
        code="privacy-zdr-2026",
        kind="trial_extension",
        days=14,
        valid_until="2026-06-24T00:00:00+00:00",
        stdout=buf,
    )
    return {"output": buf.getvalue()[-2000:]}
