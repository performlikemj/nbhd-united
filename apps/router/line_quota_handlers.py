"""State-transition handlers for the LINE Push quota state machine.

Three handlers, each idempotent. They're dispatched out-of-band (via
QStash) from two places:

  1. The daily ``poll_line_quota_task`` — emits transitions when usage
     thresholds are crossed.
  2. The 429 tripwire in the Push send paths — emits "exhausted"
     immediately when the cap is hit between polls.

Idempotency is enforced by comparing ``*_notified_at`` timestamps in
:class:`LineQuotaState` against the corresponding event timestamp.
Repeat dispatches for the same event are no-ops.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

from apps.router.line_quota import (
    clear_user_flipped_flag,
    mark_user_flipped_by_quota,
    was_user_flipped_by_quota,
)
from apps.router.models import LineQuotaState
from apps.tenants.models import Tenant, User

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Pre-warn — 90% threshold, platform owner only
# ─────────────────────────────────────────────────────────────────────


def handle_pre_warn() -> bool:
    """Email the platform owner that LINE Push is at >=90% of the
    monthly cap. Idempotent — only fires once per crossing.

    Returns True iff the email was sent. False means the email was
    skipped (already sent for this crossing, or owner email not
    configured, or state preconditions not met)."""
    state = LineQuotaState.get()

    if not state.line_quota_limit or state.line_quota_used < state.line_quota_limit * 0.90:
        logger.info("line_quota: pre-warn skipped — usage dropped below threshold")
        return False
    if state.line_quota_pre_warn_sent_at is not None:
        return False  # already sent this cycle

    owner_email = getattr(settings, "PLATFORM_OWNER_EMAIL", "")
    if not owner_email:
        logger.warning("line_quota: PLATFORM_OWNER_EMAIL not set — pre-warn skipped")
        return False

    used_pct = int(round(state.line_quota_used / state.line_quota_limit * 100))
    ctx = {
        "used": state.line_quota_used,
        "limit": state.line_quota_limit,
        "used_pct": used_pct,
    }
    subject = render_to_string("email/line_quota/pre_warn_owner_subject.txt", ctx).strip()
    body = render_to_string("email/line_quota/pre_warn_owner_body.txt", ctx)

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=None,
            recipient_list=[owner_email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("line_quota: pre-warn email send failed")
        return False

    state.line_quota_pre_warn_sent_at = timezone.now()
    state.save(update_fields=["line_quota_pre_warn_sent_at", "updated_at"])
    return True


# ─────────────────────────────────────────────────────────────────────
# Exhaustion — fan-out per LINE-preferring tenant
# ─────────────────────────────────────────────────────────────────────


def handle_exhausted() -> dict:
    """For each tenant with ``preferred_channel='line'``:

      - If ``telegram_chat_id`` is set: flip ``preferred_channel`` to
        ``telegram``, mark ``preferences.channel_flipped_by_quota=True``,
        send the "moved to Telegram" email.
      - Otherwise: send the "connect Telegram to keep getting messages"
        email; leave preference alone.

    Idempotent: bails if ``line_quota_exhausted_notified_at >=
    line_quota_exhausted_at``. Per-tenant errors are logged and skipped
    so one bad user record can't gate the fleet.

    Returns a small audit dict (flipped, emailed_line_only, errors).
    """
    state = LineQuotaState.get()
    if not state.is_exhausted:
        logger.info("line_quota: exhaustion handler called but state is not exhausted — skip")
        return {"flipped": 0, "emailed_line_only": 0, "errors": 0, "skipped": "not_exhausted"}

    if (
        state.line_quota_exhausted_notified_at is not None
        and state.line_quota_exhausted_notified_at >= state.line_quota_exhausted_at
    ):
        return {"flipped": 0, "emailed_line_only": 0, "errors": 0, "skipped": "already_notified"}

    frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
    settings_url = f"{frontend_url}/settings"

    affected = User.objects.filter(
        preferred_channel="line",
        line_user_id__isnull=False,
        tenant__status=Tenant.Status.ACTIVE,
    ).select_related("tenant")

    flipped = 0
    emailed_line_only = 0
    errors = 0

    for user in affected:
        if not user.email:
            # No address to email; skip — they'll see the gate next
            # time they open the dashboard.
            continue
        try:
            ctx = {"display_name": user.display_name or "there", "settings_url": settings_url}
            if user.telegram_chat_id:
                # Flip first so any subsequent proactive send routes to
                # Telegram even if the email send fails partway.
                user.preferred_channel = "telegram"
                user.save(update_fields=["preferred_channel"])
                mark_user_flipped_by_quota(user)

                subject = render_to_string("email/line_quota/user_flipped_to_telegram_subject.txt", ctx).strip()
                body = render_to_string("email/line_quota/user_flipped_to_telegram_body.txt", ctx)
                send_mail(
                    subject=subject,
                    message=body,
                    from_email=None,
                    recipient_list=[user.email],
                    fail_silently=False,
                )
                flipped += 1
            else:
                subject = render_to_string("email/line_quota/user_line_only_affected_subject.txt", ctx).strip()
                body = render_to_string("email/line_quota/user_line_only_affected_body.txt", ctx)
                send_mail(
                    subject=subject,
                    message=body,
                    from_email=None,
                    recipient_list=[user.email],
                    fail_silently=False,
                )
                emailed_line_only += 1
        except Exception:
            logger.exception(
                "line_quota: exhaustion fan-out failed for user %s",
                str(user.id)[:8],
            )
            errors += 1

    state.line_quota_exhausted_notified_at = timezone.now()
    # Clear any stale recovery marker now that we're back in exhausted.
    state.line_quota_recovered_notified_at = None
    state.save(
        update_fields=[
            "line_quota_exhausted_notified_at",
            "line_quota_recovered_notified_at",
            "updated_at",
        ]
    )
    logger.warning(
        "line_quota: exhaustion fan-out complete (flipped=%d, line_only=%d, errors=%d)",
        flipped,
        emailed_line_only,
        errors,
    )
    return {"flipped": flipped, "emailed_line_only": emailed_line_only, "errors": errors}


# ─────────────────────────────────────────────────────────────────────
# Recovery — fan-out per flipped tenant
# ─────────────────────────────────────────────────────────────────────


def handle_recovered() -> dict:
    """When LINE Push has headroom again, email every tenant whose
    ``preferences.channel_flipped_by_quota`` is True with a "want to
    switch back?" prompt linking to Settings. Does **not** silently
    flip them back — per the design, the user decides.

    The flag is cleared after the email (whether they click or not)
    so the next month's exhaustion event fires fresh.

    Idempotent: bails if a recovery notification was already sent
    after the most recent exhaustion event.
    """
    state = LineQuotaState.get()
    if state.is_exhausted:
        logger.info("line_quota: recovery handler called but state is still exhausted — skip")
        return {"emailed": 0, "errors": 0, "skipped": "still_exhausted"}

    # We only fire recovery if there was a *prior* exhaustion event we
    # haven't yet acknowledged. The right invariant compares against
    # ``exhausted_notified_at`` (timestamp of last fan-out) rather than
    # ``exhausted_at`` (which the poll clears on recovery — so it'd be
    # None right when we're trying to fire).
    if state.line_quota_exhausted_notified_at is None:
        # Never exhausted, nothing to recover from.
        return {"emailed": 0, "errors": 0, "skipped": "no_prior_exhaustion"}
    if (
        state.line_quota_recovered_notified_at is not None
        and state.line_quota_recovered_notified_at >= state.line_quota_exhausted_notified_at
    ):
        return {"emailed": 0, "errors": 0, "skipped": "already_notified"}

    frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
    settings_url = f"{frontend_url}/settings"

    # Find users we flipped — preferences JSON contains the key.
    # The Postgres JSON-key filter is exact-match on the string "true",
    # which our boolean True serializes to.
    flipped_users = User.objects.filter(
        preferences__channel_flipped_by_quota=True,
        tenant__status=Tenant.Status.ACTIVE,
    ).select_related("tenant")

    emailed = 0
    errors = 0
    for user in flipped_users:
        if not user.email:
            clear_user_flipped_flag(user)
            continue
        if not was_user_flipped_by_quota(user):
            # Race: flag cleared between query + save. Skip.
            continue
        try:
            ctx = {"display_name": user.display_name or "there", "settings_url": settings_url}
            subject = render_to_string("email/line_quota/user_line_recovered_subject.txt", ctx).strip()
            body = render_to_string("email/line_quota/user_line_recovered_body.txt", ctx)
            send_mail(
                subject=subject,
                message=body,
                from_email=None,
                recipient_list=[user.email],
                fail_silently=False,
            )
            emailed += 1
        except Exception:
            logger.exception(
                "line_quota: recovery fan-out failed for user %s",
                str(user.id)[:8],
            )
            errors += 1
            # Don't clear the flag on error — let the next dispatch retry.
            continue
        clear_user_flipped_flag(user)

    state.line_quota_recovered_notified_at = timezone.now()
    state.save(update_fields=["line_quota_recovered_notified_at", "updated_at"])
    logger.info("line_quota: recovery fan-out complete (emailed=%d, errors=%d)", emailed, errors)
    return {"emailed": emailed, "errors": errors}


# ─────────────────────────────────────────────────────────────────────
# Dispatcher — single entrypoint called from the QStash task wrapper
# ─────────────────────────────────────────────────────────────────────


def dispatch_for_current_state() -> dict:
    """Read the current state and fire all three handlers — each one is
    idempotent and short-circuits when there's nothing to do, so the
    dispatcher itself stays dumb. Called from
    ``dispatch_line_quota_handler_task`` (enqueued by the 429 tripwire
    and by the daily poll on transitions). Safe to call repeatedly.
    """
    return {
        "pre_warn": handle_pre_warn(),
        "exhausted": handle_exhausted(),
        "recovered": handle_recovered(),
    }
