"""Platform-agnostic lesson approval notifications.

Sends lesson approval prompts with inline buttons to the user's preferred
platform (Telegram or LINE). Follows the same pattern as
apps/actions/messaging.py for gate confirmations.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.tenants.models import Tenant

from .models import Lesson

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _send_telegram_lesson(tenant: Tenant, lesson: Lesson) -> bool:
    """Send a Telegram message with approve/dismiss inline buttons."""
    import httpx

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        return False

    chat_id = tenant.user.telegram_chat_id
    if not chat_id:
        return False

    from apps.pii.redactor import rehydrate_for_tenant

    lesson_text = rehydrate_for_tenant(tenant, lesson.text)

    from apps.router.telegram_format import render_telegram_html

    text = f'\U0001f4a1 *Something worth remembering:*\n\n"{lesson_text}"'
    rendered = render_telegram_html(text)
    body = rendered[0] if rendered else text
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "\u2705 Add to constellation", "callback_data": f"lesson:approve:{lesson.id}"},
                {"text": "\u274c Skip", "callback_data": f"lesson:dismiss:{lesson.id}"},
            ]
        ]
    }

    try:
        resp = httpx.post(
            f"{TELEGRAM_API_BASE}{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": body,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        # Retry without Markdown on failure
        resp2 = httpx.post(
            f"{TELEGRAM_API_BASE}{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f'Something worth remembering:\n\n"{lesson_text}"',
                "reply_markup": keyboard,
            },
            timeout=10,
        )
        return resp2.status_code == 200
    except Exception:
        logger.exception("Failed to send lesson notification via Telegram for tenant %s", tenant.id)
        return False


# ---------------------------------------------------------------------------
# LINE
# ---------------------------------------------------------------------------


def _send_line_lesson(tenant: Tenant, lesson: Lesson) -> bool:
    """Send a LINE Flex Message with approve/dismiss postback buttons."""
    import httpx

    channel_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_token:
        return False

    line_user_id = tenant.user.line_user_id
    if not line_user_id:
        return False

    from apps.pii.redactor import rehydrate_for_tenant
    from apps.router.line_flex import _strip_md_inline

    # LINE Flex renders no markdown — strip inline markers so the lesson
    # (AI-authored, may contain **bold**/[links]) shows clean, not raw syntax.
    lesson_text = _strip_md_inline(rehydrate_for_tenant(tenant, lesson.text))

    flex_content = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "\U0001f4a1 Something worth remembering",
                    "weight": "bold",
                    "size": "md",
                },
                {
                    "type": "text",
                    "text": lesson_text,
                    "wrap": True,
                    "margin": "md",
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#0D9488",
                    "action": {
                        "type": "postback",
                        "label": "\u2705 Add to constellation",
                        "data": f"lesson:approve:{lesson.id}",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "postback",
                        "label": "\u274c Skip",
                        "data": f"lesson:dismiss:{lesson.id}",
                    },
                },
            ],
        },
    }

    try:
        resp = httpx.post(
            LINE_PUSH_URL,
            json={
                "to": line_user_id,
                "messages": [
                    {
                        "type": "flex",
                        "altText": f'Lesson: "{lesson_text[:40]}..."',
                        "contents": flex_content,
                    }
                ],
            },
            headers={
                "Authorization": f"Bearer {channel_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                "LINE lesson push failed (%s): %s",
                resp.status_code,
                resp.text[:300],
            )
        return resp.status_code == 200
    except Exception:
        logger.exception("Failed to send lesson notification via LINE for tenant %s", tenant.id)
        return False


# ---------------------------------------------------------------------------
# Platform dispatcher
# ---------------------------------------------------------------------------

_SENDERS = {
    "telegram": _send_telegram_lesson,
    "line": _send_line_lesson,
}


def _send_app_lesson(tenant: Tenant, lesson: Lesson) -> bool:
    """Notification-only lesson delivery for an iOS-only user.

    Telegram/LINE are being decommissioned (Phase 1 — see
    ``CONTINUITY_channel_decommission.md``), so there are no approve/skip buttons
    on the app path (an iOS-parity follow-up per decision D2). The APNs push +
    ``?since=`` feed row written by ``record_proactive_outbound`` ARE the
    delivery. ``lesson.text`` is passed placeholder-space; the chokepoint
    rehydrates only for the owner-facing push and stores it placeholder-space.
    Returns True if the row was written.
    """
    from apps.router.proactive_context import record_proactive_outbound

    lesson_text = (lesson.text or "").strip()
    text = f"\U0001f4a1 Something worth remembering:\n\n“{lesson_text}”\n\nOpen NBHD to add it to your constellation."
    row = record_proactive_outbound(
        tenant=tenant,
        channel="app",
        channel_user_id=str(tenant.user_id),
        message_text=text,
        job_name="lesson_approval",
    )
    return row is not None


def send_lesson_approval_buttons(tenant: Tenant, lesson: Lesson) -> bool:
    """Send a pending-lesson approval prompt to the user's delivery channel.

    Channel selection goes through the canonical ``resolve_user_channel``:
    iOS-only users (the common case now) get a notification-only app push
    (:func:`_send_app_lesson`); Telegram/LINE-linked users fall through to the
    (decommissioned, unreachable-by-resolver) ``_SENDERS`` senders. Returns True
    if delivered.
    """
    from apps.router.cron_delivery import resolve_user_channel

    channel = resolve_user_channel(tenant.user)
    if channel == "app":
        return _send_app_lesson(tenant, lesson)

    sender = _SENDERS.get(channel)
    if sender and sender(tenant, lesson):
        return True

    logger.warning("Could not send lesson notification for tenant %s on any channel", tenant.id)
    return False
