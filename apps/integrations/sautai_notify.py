"""sautai Phase 0 completion notification — the meditation-style "ready" ping.

Clones ``apps.core.services.notify_meditation_ready``'s shape: resolve the
tenant's linked channel, send a short "your meal plan is ready" message
(Telegram/LINE/app), then record it through ``record_proactive_outbound`` —
the single chokepoint that fires the APNs push + writes the ``?since=`` feed
row, idempotent on ``ProactiveOutbound.notified_at``. See
docs/sautai-phase0-contract.md.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.tenants.models import Tenant

from .models import SautaiMealPlanJob

logger = logging.getLogger(__name__)

_READY_JOB_NAME = "_sautai:plan_ready"


def notify_sautai_plan_ready(job: SautaiMealPlanJob) -> bool:
    """Send a short "your meal plan is ready" ping to the tenant's channel.

    Returns True if a message was delivered. The plan is already stored on
    ``job`` (status=READY), so any failure here is logged and swallowed —
    never propagated back into the QStash task, which must not re-flip a
    READY job over a notify hiccup and trigger a wasted re-generation.
    """
    tenant = job.tenant
    if tenant.status != Tenant.Status.ACTIVE:
        logger.info("sautai notify skipped: tenant %s not active (%s)", str(tenant.id)[:8], tenant.status)
        return False

    user = getattr(tenant, "user", None)
    if user is None:
        return False

    from apps.router.cron_delivery import resolve_user_channel

    channel = resolve_user_channel(user)
    if channel is None:
        logger.info("sautai notify skipped: tenant %s has no linked channel", str(tenant.id)[:8])
        return False

    message = _ready_message(job)

    if channel == "line":
        channel_user_id = getattr(user, "line_user_id", "") or ""
        delivered = _send_line_text(channel_user_id, message)
    elif channel in ("app", "eval"):
        # App records a user-visible feed row and APNs push. Eval records only
        # internal evidence; the recorder suppresses APNs for that channel.
        channel_user_id = str(user.id)
        delivered = True
    else:
        chat_id = getattr(user, "telegram_chat_id", None)
        channel_user_id = str(chat_id or "")
        delivered = bool(chat_id) and _send_telegram_text(chat_id, message)

    if delivered and channel_user_id:
        try:
            from apps.router.proactive_context import record_proactive_outbound

            # message contains no PII (week/link only) — no rehydrate split
            # needed, unlike core.services.notify_meditation_ready's title.
            record_proactive_outbound(
                tenant=tenant,
                channel=channel,
                channel_user_id=channel_user_id,
                message_text=message,
                job_name=_READY_JOB_NAME,
            )
        except Exception:
            # For app-channel (iOS-only) users this record IS the delivery — the
            # APNs push + ?since= feed row are written here — so a swallowed
            # failure means a READY job with no push. Warn, don't hide it at debug.
            logger.warning("sautai notify: proactive record failed for job %s", job.id, exc_info=True)

    return bool(delivered)


def _ready_message(job: SautaiMealPlanJob) -> str:
    """Build the "meal plan ready" body, with Phase 0.5 funnel copy.

    Attribution is always present ("powered by sautai"). For an UNCLAIMED account
    (``account_claimed is False``) the CTA is the claim link + plan count — the
    sautai funnel — instead of the plain web link. For a claimed/linked account
    the plain web link stays. If a plan already existed for the week and the user
    had given fresh guidance we didn't regenerate on, add an honest nudge rather
    than implying the plan is freshly tailored.
    """
    plan = job.result if isinstance(job.result, dict) else {}
    funnel = job.funnel if isinstance(job.funnel, dict) else {}
    week_start = plan.get("week_start") or (job.week_start.isoformat() if job.week_start else "")

    base = f"Your meal plan for the week of {week_start} is ready" if week_start else "Your meal plan is ready"
    text = f"{base} — powered by sautai."

    account_claimed = funnel.get("account_claimed")
    claim_link = (funnel.get("claim_link") or "").strip()
    plan_count = funnel.get("plan_count")
    web_link = (job.web_link or "").strip()

    if account_claimed is False and claim_link:
        if isinstance(plan_count, int) and plan_count > 1:
            text += f" You've created {plan_count} plans — claim your sautai account to see them all: {claim_link}"
        else:
            text += f" Claim your sautai account to keep it and view it any time: {claim_link}"
    elif web_link:
        text += f" View it: {web_link}"

    if funnel.get("already_existed") and (job.user_prompt or "").strip() and not job.regenerate:
        text += (
            " (This was your existing plan for the week — ask me to regenerate it "
            "if you want your latest notes applied.)"
        )

    return text


def _send_telegram_text(chat_id: int, text: str) -> bool:
    from apps.router.services import send_telegram_message

    return send_telegram_message(chat_id, text)


def _send_line_text(line_user_id: str, text: str) -> bool:
    if not line_user_id:
        return False
    access_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "")
    if not access_token:
        logger.warning("sautai notify: LINE_CHANNEL_ACCESS_TOKEN not configured")
        return False

    import httpx

    messages = [{"type": "text", "text": text[:4900]}]
    try:
        resp = httpx.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"to": line_user_id, "messages": messages},
            timeout=10,
        )
    except Exception:
        logger.exception("sautai notify: LINE push error")
        return False
    if resp.status_code >= 300:
        logger.warning("sautai notify: LINE push failed status=%s", resp.status_code)
        return False
    return True
