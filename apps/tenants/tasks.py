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


def send_ios_relaunch_campaign_task() -> dict:
    """QStash-dispatched wrapper around ``send_promo_campaign`` — the iOS
    relaunch win-back (14 days free, now that NBHD is on the App Store).

    Zero-arg by contract (QStash MCP body passthrough is unreliable), so the
    campaign constants are inlined. This re-sends to the same lapsed /
    active-trial cohort as ``privacy-zdr-2026`` — which closed 2026-06-24 with
    **0 redemptions** (the blast almost certainly landed in spam) — but with:

      - a fresh, unique ``code`` → a new audience snapshot and brand-new
        per-user redemption rows, so anyone who saw the prior offer can still
        claim this one;
      - a new redemption window (``valid_until``);
      - a refreshed subject + the iOS-relaunch template (leads with the App
        Store launch, keeps the click-to-extend-trial mechanic unchanged).

    Re-running is safe: ``get_or_create(code=...)`` reuses the campaign row and
    re-emails the original snapshot; double-redemption is blocked at the view
    layer by ``unique_together(campaign, user)``.
    """
    from io import StringIO

    from django.core.management import call_command

    buf = StringIO()
    call_command(
        "send_promo_campaign",
        code="ios-relaunch-2026-06",
        kind="trial_extension",
        days=14,
        valid_until="2026-07-02T00:00:00+00:00",
        template_base="email/ios_relaunch_2026/email",
        stdout=buf,
    )
    return {"output": buf.getvalue()[-2000:]}


def send_comeback_campaign_task() -> dict:
    """QStash-dispatched wrapper around ``send_promo_campaign`` — the July 2026
    comeback win-back (free trial extension + App Store).

    Zero-arg by contract (QStash MCP body passthrough is unreliable), so the
    campaign constants are inlined. Distinct from the prior sends in two ways:

      - ``audience="comeback"`` widens the cohort to *every* onboarded,
        has-messaged tenant that is ACTIVE or SUSPENDED — deliberately
        including paid-then-lapsed tenants (SUSPENDED with a retained
        stripe_subscription_id) that the earlier trial-only audience dropped.
        Redeeming now also restores the runtime for those suspended tenants
        (see restore_tenant_runtime), so a lapsed subscriber who claims the
        offer gets a working container, not silence.
      - the ``email/comeback_2026_07`` template carries the new structure
        (offer-first hero, App Store block, personality + privacy sections)
        and every marketing send now sets List-Unsubscribe headers, so this
        blast has a real unsubscribe path the prior ones lacked.

    Re-running is safe: ``get_or_create(code=...)`` reuses the campaign row, but
    the send loop re-queries the LIVE audience each run and emails that (the
    ``audience_snapshot`` on the row is a frozen first-run audit only, not the
    send list) — so users who entered the eligible state after the first run
    also get the mail. Double-redemption is blocked at the view layer by
    ``unique_together(campaign, user)``. Opted-out users are excluded.
    """
    from io import StringIO

    from django.core.management import call_command

    buf = StringIO()
    call_command(
        "send_promo_campaign",
        code="comeback-2026-07",
        kind="trial_extension",
        days=14,
        valid_until="2026-07-20T00:00:00+00:00",
        template_base="email/comeback_2026_07/email",
        audience="comeback",
        stdout=buf,
    )
    return {"output": buf.getvalue()[-2000:]}
