"""Tasks for tenant maintenance (executed via QStash)."""

import logging
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from django.conf import settings
from django.utils import timezone
from django.utils.http import urlencode

from .models import Tenant
from .promo_models import PromoCampaign, PromoCampaignSend
from .promo_signing import make_promo_token
from .services import reset_daily_counters, reset_monthly_counters
from .telegram_models import TelegramLinkToken

logger = logging.getLogger(__name__)

COMEBACK_2026_08_CODE = "comeback-2026-08"
COMEBACK_2026_08_EXTENSION_DAYS = 30
COMEBACK_2026_08_VALID_UNTIL = datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)
COMEBACK_2026_08_TEMPLATE_BASE = "email/comeback_2026_08/email"


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


def send_comeback_2026_08_campaign_task(tenant_ids: list[str]) -> dict:
    """Send the August comeback offer only to an operator-supplied audience.

    ``tenant_ids`` is the frozen targeting decision. This task deliberately
    performs no live audience discovery: it fetches only those primary keys,
    re-checks every safety/suppression condition immediately before delivery,
    and claims a durable per-(campaign, user) marker before sending.

    Fire through the generic QStash-signed trigger with this body::

        {"kwargs": {"tenant_ids": ["<tenant-uuid>", "..."]}}
    """
    from apps.tenants.management.commands.send_promo_campaign import Command

    if not isinstance(tenant_ids, list) or not tenant_ids:
        raise ValueError("tenant_ids must be a non-empty list of tenant UUID strings")

    normalized_ids: list[str] = []
    seen_ids: set[str] = set()
    duplicate_ids_ignored = 0
    for value in tenant_ids:
        try:
            normalized = str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Invalid tenant UUID in tenant_ids: {value!r}") from exc
        if normalized in seen_ids:
            duplicate_ids_ignored += 1
            continue
        seen_ids.add(normalized)
        normalized_ids.append(normalized)

    tenants = {str(tenant.id): tenant for tenant in Tenant.objects.filter(id__in=normalized_ids).select_related("user")}
    snapshot = {
        "tenant_ids": normalized_ids,
        "user_ids": [str(tenants[tenant_id].user_id) for tenant_id in normalized_ids if tenant_id in tenants],
        "captured_at_count": len(normalized_ids),
        "audience_mode": "targeted_tenant_ids",
    }
    campaign, created = PromoCampaign.objects.get_or_create(
        code=COMEBACK_2026_08_CODE,
        defaults={
            "kind": PromoCampaign.Kind.TRIAL_EXTENSION,
            "extension_days": COMEBACK_2026_08_EXTENSION_DAYS,
            "valid_until": COMEBACK_2026_08_VALID_UNTIL,
            "audience_snapshot": snapshot,
        },
    )

    if not created:
        expected_config = (
            PromoCampaign.Kind.TRIAL_EXTENSION,
            COMEBACK_2026_08_EXTENSION_DAYS,
            COMEBACK_2026_08_VALID_UNTIL,
        )
        actual_config = (campaign.kind, campaign.extension_days, campaign.valid_until)
        if actual_config != expected_config:
            raise ValueError(
                f"Existing {COMEBACK_2026_08_CODE} campaign configuration does not match the task constants"
            )

        frozen_ids = campaign.audience_snapshot.get("tenant_ids")
        if not isinstance(frozen_ids, list) or set(frozen_ids) != set(normalized_ids):
            raise ValueError(f"Existing {COMEBACK_2026_08_CODE} audience does not match the supplied tenant_ids")

    sender = Command()
    frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
    skip_reasons: Counter[str] = Counter()
    sent = 0

    def skip(tenant_id: str, reason: str) -> None:
        skip_reasons[reason] += 1
        logger.info(
            "comeback_2026_08 tenant outcome — tenant=%s outcome=skipped reason=%s",
            tenant_id,
            reason,
        )

    for tenant_id in normalized_ids:
        tenant = tenants.get(tenant_id)
        if tenant is None:
            skip(tenant_id, "tenant_not_found")
            continue
        if tenant.stripe_subscription_id:
            skip(tenant_id, "stripe_subscription_id")
            continue
        if tenant.is_synthetic:
            skip(tenant_id, "is_synthetic")
            continue
        if tenant.is_eval_sink:
            skip(tenant_id, "is_eval_sink")
            continue
        if tenant.pending_deletion:
            skip(tenant_id, "pending_deletion")
            continue
        if tenant.status not in (Tenant.Status.SUSPENDED, Tenant.Status.ACTIVE):
            skip(tenant_id, "ineligible_status")
            continue

        user = tenant.user
        if user.email_opt_out:
            skip(tenant_id, "email_opt_out")
            continue
        if not user.email:
            skip(tenant_id, "missing_email")
            continue

        send_marker, claimed = PromoCampaignSend.objects.get_or_create(
            campaign=campaign,
            user=user,
        )
        if not claimed:
            skip(tenant_id, "already_sent")
            continue

        token = make_promo_token(campaign.code, user.id)
        query = urlencode({"code": campaign.code, "token": token})
        promo_url = f"{frontend_url}/promo/redeem?{query}"

        try:
            # Reuse the July command's exact rendering, unsubscribe URL, and
            # List-Unsubscribe header behavior rather than creating a second
            # marketing-email implementation.
            sender._send_promo_email(
                user,
                promo_url=promo_url,
                unsubscribe_url=sender._unsubscribe_url(user),
                valid_until=campaign.valid_until,
                template_base=COMEBACK_2026_08_TEMPLATE_BASE,
            )
        except Exception:
            # A handled delivery failure is retryable on the next operator
            # fire. Successful sends keep the claim, preventing duplicates.
            send_marker.delete()
            skip(tenant_id, "email_failed")
            logger.exception(
                "comeback_2026_08 email delivery failed — tenant=%s campaign=%s",
                tenant_id,
                campaign.code,
            )
            continue

        sent += 1
        logger.info(
            "comeback_2026_08 tenant outcome — tenant=%s outcome=sent",
            tenant_id,
        )

    result = {
        "campaign": campaign.code,
        "targeted": len(normalized_ids),
        "sent": sent,
        "skipped": sum(skip_reasons.values()),
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "duplicate_ids_ignored": duplicate_ids_ignored,
    }
    logger.info("comeback_2026_08 campaign outcome counts — %s", result)
    return result
