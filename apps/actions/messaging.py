"""Platform-agnostic gate confirmation messaging.

Sends confirmation prompts with inline buttons to the user's preferred
platform (Telegram or LINE), and edits the message after response.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.tenants.models import Tenant

from .models import ActionStatus, PendingAction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _send_telegram_confirmation(tenant: Tenant, action: PendingAction) -> str | None:
    """Send a Telegram message with inline approve/deny buttons.

    Returns the Telegram message_id (str) on success, None on failure.
    """
    import httpx

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.warning("Cannot send gate confirmation: no Telegram bot token")
        return None

    chat_id = tenant.user.telegram_chat_id
    if not chat_id:
        logger.warning("Tenant %s has no Telegram chat_id", tenant.id)
        return None

    text = (
        "⚠️ *Action Confirmation Required*\n\n"
        f"Your agent wants to:\n"
        f"*{_escape_markdown(action.display_summary)}*\n\n"
        "This action cannot be undone\\.\n\n"
        "_Expires in 5 minutes_"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"gate_approve:{action.id}"},
                {"text": "❌ Deny", "callback_data": f"gate_deny:{action.id}"},
            ]
        ]
    }

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "reply_markup": keyboard,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return str(data.get("result", {}).get("message_id", ""))
        else:
            # Fall back to plain text if Markdown fails
            resp2 = httpx.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        "⚠️ Action Confirmation Required\n\n"
                        f"Your agent wants to:\n"
                        f"{action.display_summary}\n\n"
                        "This action cannot be undone.\n\n"
                        "Expires in 5 minutes"
                    ),
                    "reply_markup": keyboard,
                },
                timeout=10,
            )
            if resp2.status_code == 200:
                data = resp2.json()
                return str(data.get("result", {}).get("message_id", ""))
            logger.warning("sendMessage failed (%s): %s", resp2.status_code, resp2.text[:200])
            return None
    except Exception:
        logger.exception("Failed to send gate confirmation for tenant %s", tenant.id)
        return None


def _edit_telegram_message(tenant: Tenant, action: PendingAction) -> None:
    """Edit the Telegram confirmation message to show result and remove buttons."""
    import httpx

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token or not action.platform_message_id:
        return

    chat_id = tenant.user.telegram_chat_id
    if not chat_id:
        return

    if action.status == ActionStatus.APPROVED:
        icon, label = "✅", "APPROVED"
    elif action.status == ActionStatus.DENIED:
        icon, label = "❌", "DENIED"
    else:
        icon, label = "⏰", "EXPIRED"

    new_text = f"{icon} Action {label}\n\n{action.display_summary}"

    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": int(action.platform_message_id),
                "text": new_text,
                "reply_markup": {"inline_keyboard": []},
            },
            timeout=10,
        )
    except Exception:
        logger.exception("Failed to edit gate message for tenant %s", tenant.id)


# ---------------------------------------------------------------------------
# LINE
# ---------------------------------------------------------------------------


def _send_line_confirmation(tenant: Tenant, action: PendingAction) -> str | None:
    """Send a LINE Flex Message with approve/deny buttons.

    Returns a placeholder message ID on success, None on failure.
    """
    import httpx

    channel_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_token:
        logger.warning("Cannot send gate confirmation: no LINE channel token")
        return None

    line_user_id = tenant.user.line_user_id
    if not line_user_id:
        logger.warning("Tenant %s has no LINE user_id", tenant.id)
        return None

    # Build Flex Message with action buttons
    flex_content = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "⚠️ Action Confirmation",
                    "weight": "bold",
                    "size": "lg",
                },
                {
                    "type": "text",
                    "text": f"Your agent wants to:\n{action.display_summary}",
                    "wrap": True,
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": "This action cannot be undone.",
                    "color": "#999999",
                    "size": "sm",
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": "Expires in 5 minutes",
                    "color": "#999999",
                    "size": "xs",
                    "margin": "sm",
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
                    "color": "#22C55E",
                    "action": {
                        "type": "postback",
                        "label": "✅ Approve",
                        "data": f"gate_approve:{action.id}",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "postback",
                        "label": "❌ Deny",
                        "data": f"gate_deny:{action.id}",
                    },
                },
            ],
        },
    }

    try:
        resp = httpx.post(
            "https://api.line.me/v2/bot/message/push",
            json={
                "to": line_user_id,
                "messages": [
                    {
                        "type": "flex",
                        "altText": f"Action confirmation: {action.display_summary[:40]}",
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
        if resp.status_code == 200:
            # LINE push API doesn't return message_id directly
            return f"line-push-{action.id}"
        logger.warning("LINE push failed (%s): %s", resp.status_code, resp.text[:200])
        return None
    except Exception:
        logger.exception("Failed to send LINE gate confirmation for tenant %s", tenant.id)
        return None


def _edit_line_message(tenant: Tenant, action: PendingAction) -> None:
    """LINE doesn't support message editing. Send a follow-up instead."""
    import httpx

    channel_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    line_user_id = tenant.user.line_user_id
    if not channel_token or not line_user_id:
        return

    if action.status == ActionStatus.APPROVED:
        icon, label = "✅", "Approved"
    elif action.status == ActionStatus.DENIED:
        icon, label = "❌", "Denied"
    else:
        icon, label = "⏰", "Expired"

    text = f"{icon} {label}: {action.display_summary}"

    try:
        httpx.post(
            "https://api.line.me/v2/bot/message/push",
            json={
                "to": line_user_id,
                "messages": [{"type": "text", "text": text}],
            },
            headers={
                "Authorization": f"Bearer {channel_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except Exception:
        logger.exception("Failed to send LINE gate result for tenant %s", tenant.id)


# ---------------------------------------------------------------------------
# Platform dispatcher
# ---------------------------------------------------------------------------

_SENDERS = {
    "telegram": (_send_telegram_confirmation, _edit_telegram_message),
    "line": (_send_line_confirmation, _edit_line_message),
}


def send_gate_confirmation(tenant: Tenant, action: PendingAction) -> None:
    """Send a confirmation prompt to the user on their preferred platform."""
    channel = getattr(tenant.user, "preferred_channel", "") or "telegram"
    sender, _ = _SENDERS.get(channel, (None, None))

    if not sender:
        logger.warning("No gate sender for channel %s (tenant %s)", channel, tenant.id)
        return

    msg_id = sender(tenant, action)
    if msg_id:
        action.platform_message_id = msg_id
        action.platform_channel = channel
        action.save(update_fields=["platform_message_id", "platform_channel"])


def update_gate_message(action: PendingAction) -> None:
    """Edit/follow-up the confirmation message to show the result."""
    if not action.platform_channel:
        return

    _, editor = _SENDERS.get(action.platform_channel, (None, None))
    if editor:
        editor(action.tenant, action)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_markdown(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters."""
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)
