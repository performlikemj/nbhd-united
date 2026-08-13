"""Platform-agnostic gate confirmation messaging.

Sends Telegram/LINE prompts with inline buttons, or a generic iOS wake for the
datebook review sheet, and updates messaging prompts after response.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.common.eval_sink import suppresses_real_transport
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
    if suppresses_real_transport(tenant):
        return None

    import httpx

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.warning("Cannot send gate confirmation: no Telegram bot token")
        return None

    chat_id = tenant.user.telegram_chat_id
    if not chat_id:
        logger.warning("Tenant %s has no Telegram chat_id", tenant.id)
        return None

    from apps.pii.redactor import rehydrate_for_tenant

    summary = rehydrate_for_tenant(tenant, action.display_summary)

    text = (
        "⚠️ *Action Confirmation Required*\n\n"
        f"Your agent wants to:\n"
        f"*{_escape_markdown(summary)}*\n\n"
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
                        f"{summary}\n\n"
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
    if suppresses_real_transport(tenant):
        return

    import httpx

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token or not action.platform_message_id:
        return

    chat_id = tenant.user.telegram_chat_id
    if not chat_id:
        return

    from apps.pii.redactor import rehydrate_for_tenant

    summary = rehydrate_for_tenant(tenant, action.display_summary)

    if action.status == ActionStatus.APPROVED:
        icon, label = "✅", "APPROVED"
    elif action.status == ActionStatus.DENIED:
        icon, label = "❌", "DENIED"
    else:
        icon, label = "⏰", "EXPIRED"

    new_text = f"{icon} Action {label}\n\n{summary}"

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
    if suppresses_real_transport(tenant):
        return None

    import httpx

    channel_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_token:
        logger.warning("Cannot send gate confirmation: no LINE channel token")
        return None

    line_user_id = tenant.user.line_user_id
    if not line_user_id:
        logger.warning("Tenant %s has no LINE user_id", tenant.id)
        return None

    from apps.pii.redactor import rehydrate_for_tenant

    summary = rehydrate_for_tenant(tenant, action.display_summary)

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
                    "text": f"Your agent wants to:\n{summary}",
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
                        "altText": f"Action confirmation: {summary[:40]}",
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
    if suppresses_real_transport(tenant):
        return

    import httpx

    channel_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    line_user_id = tenant.user.line_user_id
    if not channel_token or not line_user_id:
        return

    from apps.pii.redactor import rehydrate_for_tenant

    summary = rehydrate_for_tenant(tenant, action.display_summary)

    if action.status == ActionStatus.APPROVED:
        icon, label = "✅", "Approved"
    elif action.status == ActionStatus.DENIED:
        icon, label = "❌", "Denied"
    else:
        icon, label = "⏰", "Expired"

    text = f"{icon} {label}: {summary}"

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
# App
# ---------------------------------------------------------------------------


def _send_app_confirmation(tenant: Tenant, action: PendingAction) -> str | None:
    """Wake the iOS review sheet once with a generic, installation-targeted push."""

    from apps.datebook.gate import is_datebook_action_type

    if not is_datebook_action_type(action.action_type):
        return None

    claim_id = f"app-gate-{action.id}"
    claimed = PendingAction.objects.filter(
        id=action.id,
        tenant=tenant,
        platform_channel="",
        platform_message_id="",
    ).update(
        platform_channel="app",
        platform_message_id=claim_id,
    )
    if not claimed:
        action.refresh_from_db(fields=["platform_channel", "platform_message_id"])
        if action.platform_channel == "app":
            return action.platform_message_id or claim_id
        return None

    action.platform_channel = "app"
    action.platform_message_id = claim_id

    try:
        from apps.datebook.models import DatebookGateway
        from apps.router.push_views import _push_to_user_devices

        installation_id = (
            DatebookGateway.objects.filter(
                tenant=tenant,
                status=DatebookGateway.Status.ACTIVE,
            )
            .values_list("installation_id", flat=True)
            .first()
        )
        _push_to_user_devices(
            tenant.user,
            body="Your assistant has a calendar request to review — open NBHD",
            thread_id=None,
            collapse_id=f"datebook-gate:{action.id}",
            content_available=True,
            extra={
                "type": "datebook_gate",
                "action_id": str(action.id),
            },
            installation_id=installation_id,
        )
    except Exception:
        logger.warning(
            "App gate push failed (non-fatal) for action %s tenant %s",
            action.id,
            tenant.id,
            exc_info=True,
        )
    return claim_id


# ---------------------------------------------------------------------------
# Platform dispatcher
# ---------------------------------------------------------------------------

_SENDERS = {
    "telegram": (_send_telegram_confirmation, _edit_telegram_message),
    "line": (_send_line_confirmation, _edit_line_message),
    "app": (_send_app_confirmation, None),
}


def _resolve_gate_channel(user, *, originating_channel: str | None = None) -> str | None:
    """Resolve the channel for an INTERACTIVE gate confirmation.

    An explicit originating channel is authoritative. ``ios`` normalizes to the
    app review surface; Telegram/LINE keep their established senders and button
    payloads. An invalid explicit value fails closed instead of crossing to a
    linked channel.

    Deliberately NOT ``resolve_user_channel`` (which is app-first): gate buttons
    stay on linked messaging channels in their established Telegram-then-LINE
    order. The app review sheet is the fallback when a datebook gateway or APNs
    token proves an app surface exists. Only datebook actions use that fallback,
    because the consumer review endpoint deliberately excludes every other gate
    type.

    Linked-Telegram-first (matching ``resolve_user_channel``'s messaging
    fallback) because it preserves prior behavior for the only both-linked cohort
    in production: the old resolver honoured ``preferred_channel`` (universally
    the "telegram" default), so a user with BOTH channels linked has always
    received gate buttons on Telegram — flipping them to LINE would silently move
    the approval surface out from under an active Telegram user. LINE next covers
    line-only users.

    ``preferred_channel`` is ignored for the same reason it is in
    ``resolve_user_channel`` (production noise — every row is the schema
    default).
    """
    if originating_channel:
        normalized = originating_channel.strip().lower()
        if normalized in {"app", "ios"}:
            return "app"
        if normalized in {"telegram", "line"}:
            return normalized
        return None

    if getattr(user, "telegram_chat_id", None):
        return "telegram"
    if getattr(user, "line_user_id", None):
        return "line"

    tenant = getattr(user, "tenant", None)
    if tenant is None:
        return None

    from apps.datebook.models import DatebookGateway
    from apps.router.models import DeviceToken

    has_gateway = DatebookGateway.objects.filter(
        tenant=tenant,
        status=DatebookGateway.Status.ACTIVE,
    ).exists()
    has_device = DeviceToken.objects.filter(
        user=user,
        revoked_at__isnull=True,
    ).exists()
    if has_gateway or has_device:
        return "app"
    return None


def send_gate_confirmation(
    tenant: Tenant,
    action: PendingAction,
    *,
    originating_channel: str | None = None,
) -> bool:
    """Send a confirmation prompt to the user on their delivery channel.

    Resolves via ``_resolve_gate_channel`` rather than the app-first proactive
    resolver. Linked Telegram/LINE surfaces retain priority; app-only users get
    a generic APNs wake for the in-app review sheet. The push contains only the
    action id/type discriminator and never carries review content or an approval
    action.

    Returns ``True`` if the confirmation was dispatched to a real channel,
    ``False`` when no deliverable channel exists.
    The caller can use the return value to decide whether to return HTTP 202
    "pending" (real channel) or indicate "undeliverable" (no channel).

    When no channel is linked, log a clear warning so the no-surface case is
    visible and diagnosable.
    """
    if suppresses_real_transport(tenant):
        # Confirmation gates require a human approve/deny surface. An eval sink
        # has none, and must never reach a transport sender. Checked on the
        # tenant flag directly because ``_resolve_gate_channel`` reads linked
        # surfaces without consulting ``resolve_user_channel`` — a stale real
        # channel on an eval tenant would otherwise emit.
        logger.info("Gate confirmation suppressed for eval-sink tenant %s", tenant.id)
        return False
    channel = _resolve_gate_channel(
        tenant.user,
        originating_channel=originating_channel,
    )
    if channel == "app":
        from apps.datebook.gate import is_datebook_action_type

        if not is_datebook_action_type(action.action_type):
            logger.warning(
                "Cannot deliver non-datebook gate action %s to app review sheet",
                action.id,
            )
            return False
    sender, _ = _SENDERS.get(channel, (None, None))

    if not sender:
        logger.warning(
            "Cannot deliver gate confirmation for action %s (tenant %s): "
            "no Telegram/LINE/app channel for resolved channel %r — action will "
            "not be delivered",
            action.id,
            tenant.id,
            channel,
        )
        return False

    msg_id = sender(tenant, action)
    if channel == "app" and not msg_id:
        return False
    if msg_id:
        action.platform_message_id = msg_id
        action.platform_channel = channel
        action.save(update_fields=["platform_message_id", "platform_channel"])
    return True


def update_gate_message(action: PendingAction) -> None:
    """Edit/follow-up the confirmation message to show the result."""
    # Re-check at result time: the action may predate an eval-sink backfill or
    # the flag may have changed after the original confirmation was sent.
    if suppresses_real_transport(action.tenant):
        return
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
