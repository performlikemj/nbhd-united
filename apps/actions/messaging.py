"""Platform-agnostic gate confirmation messaging.

Sends Telegram/LINE prompts with inline buttons, claims the durable iOS review
surface, and updates messaging prompts after response. Datebook invalidation
pushes live in ``apps.datebook.notify``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings

from apps.common.eval_sink import suppresses_real_transport
from apps.tenants.models import Tenant

from .models import ActionStatus, PendingAction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateSendResult:
    """Transport acceptance and its independently truthful platform message id."""

    accepted: bool
    platform_message_id: str = ""


def _review_window_text(action: PendingAction, *, markdown: bool = False) -> str:
    from apps.cron.gate import is_cron_action_type
    from apps.datebook.gate import is_datebook_action_type

    action_type = getattr(action, "action_type", "")
    if is_datebook_action_type(action_type):
        text = "Review within 24 hours"
    elif is_cron_action_type(action_type):
        text = "Review within 72 hours"
    else:
        text = "Expires in 5 minutes"
    return f"_{text}_" if markdown else text


def _is_cron_create(action: PendingAction) -> bool:
    from apps.cron.gate import is_cron_action_type

    return is_cron_action_type(getattr(action, "action_type", ""))


def _confirmation_consequence(action: PendingAction, *, markdown: bool = False) -> str:
    text = "You can disable this scheduled task later." if _is_cron_create(action) else "This action cannot be undone."
    return _escape_markdown(text) if markdown else text


def _result_label(action: PendingAction) -> tuple[str, str]:
    if action.status == ActionStatus.APPROVED and _is_cron_create(action):
        if action.resolution_code == "executed":
            return "✅", "CREATED"
        if action.resolution_code.startswith("create_failed") or action.resolution_code == "dispatch_failed":
            return "⚠️", f"APPROVED — CREATION FAILED ({action.resolution_code})"
        return "⏳", "APPROVED — CREATION QUEUED"
    if action.status == ActionStatus.APPROVED:
        return "✅", "APPROVED"
    if action.status == ActionStatus.DENIED:
        return "❌", "DENIED"
    return "⏰", "EXPIRED"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _send_telegram_confirmation(tenant: Tenant, action: PendingAction) -> GateSendResult:
    """Send a Telegram message with inline approve/deny buttons.

    A 200 without a real Telegram message id is accepted but never called sent.
    """
    if suppresses_real_transport(tenant):
        return GateSendResult(False)

    import httpx

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.warning("Cannot send gate confirmation: no Telegram bot token")
        return GateSendResult(False)

    chat_id = tenant.user.telegram_chat_id
    if not chat_id:
        logger.warning("Tenant %s has no Telegram chat_id", tenant.id)
        return GateSendResult(False)

    from apps.pii.redactor import rehydrate_for_tenant

    summary = rehydrate_for_tenant(tenant, action.display_summary)

    text = (
        "⚠️ *Action Confirmation Required*\n\n"
        f"Your agent wants to:\n"
        f"*{_escape_markdown(summary)}*\n\n"
        f"{_confirmation_consequence(action, markdown=True)}\n\n"
        f"{_review_window_text(action, markdown=True)}"
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
            return GateSendResult(True, str(data.get("result", {}).get("message_id") or ""))
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
                        f"{_confirmation_consequence(action)}\n\n" + _review_window_text(action)
                    ),
                    "reply_markup": keyboard,
                },
                timeout=10,
            )
            if resp2.status_code == 200:
                data = resp2.json()
                return GateSendResult(True, str(data.get("result", {}).get("message_id") or ""))
            logger.warning("sendMessage failed (%s): %s", resp2.status_code, resp2.text[:200])
            return GateSendResult(False)
    except Exception:
        logger.exception("Failed to send gate confirmation for tenant %s", tenant.id)
        return GateSendResult(False)


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

    icon, label = _result_label(action)

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


def _send_line_confirmation(tenant: Tenant, action: PendingAction) -> GateSendResult:
    """Send a LINE Flex Message with approve/deny buttons.

    LINE acceptance is distinct from a real response ``sentMessages[].id``.
    """
    if suppresses_real_transport(tenant):
        return GateSendResult(False)

    import httpx

    channel_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_token:
        logger.warning("Cannot send gate confirmation: no LINE channel token")
        return GateSendResult(False)

    line_user_id = tenant.user.line_user_id
    if not line_user_id:
        logger.warning("Tenant %s has no LINE user_id", tenant.id)
        return GateSendResult(False)

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
                    "text": _confirmation_consequence(action),
                    "color": "#999999",
                    "size": "sm",
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": _review_window_text(action),
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
            try:
                sent_messages = resp.json().get("sentMessages") or []
                message_id = str(sent_messages[0].get("id") or "") if sent_messages else ""
            except (AttributeError, IndexError, TypeError, ValueError):
                message_id = ""
            return GateSendResult(True, message_id)
        logger.warning("LINE push failed (%s): %s", resp.status_code, resp.text[:200])
        return GateSendResult(False)
    except Exception:
        logger.exception("Failed to send LINE gate confirmation for tenant %s", tenant.id)
        return GateSendResult(False)


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

    icon, label = _result_label(action)
    label = label.title()

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


def _send_app_confirmation(tenant: Tenant, action: PendingAction) -> GateSendResult:
    """Claim the durable in-app surface; gate-changed owns the APNs wake."""

    from apps.cron.gate import is_cron_action_type
    from apps.datebook.gate import is_datebook_action_type

    if not (is_datebook_action_type(action.action_type) or is_cron_action_type(action.action_type)):
        return GateSendResult(False)
    if is_cron_action_type(action.action_type):
        from apps.datebook.models import DatebookGateway

        if not DatebookGateway.objects.filter(
            tenant=tenant,
            status=DatebookGateway.Status.ACTIVE,
        ).exists():
            return GateSendResult(False)
    return GateSendResult(True)


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
    the durable in-app review surface. The independent, PII-free
    ``datebook_gate_changed`` notification wakes that surface.

    Returns ``True`` when a confirmation surface was selected and invoked,
    ``False`` when no deliverable surface exists. Transport acceptance and real
    message IDs are persisted separately in ``delivery_state``.
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
        from apps.cron.gate import is_cron_action_type
        from apps.datebook.gate import is_datebook_action_type

        if not (is_datebook_action_type(action.action_type) or is_cron_action_type(action.action_type)):
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

    raw_result = sender(tenant, action)
    if isinstance(raw_result, GateSendResult):
        send_result = raw_result
    elif isinstance(raw_result, str):
        # Compatibility for injected/custom senders while the built-ins use the
        # explicit accepted/id contract above.
        send_result = GateSendResult(bool(raw_result), raw_result)
    else:
        send_result = GateSendResult(False)

    action.platform_channel = channel
    action.platform_message_id = send_result.platform_message_id
    if channel == "app" and send_result.accepted:
        action.delivery_state = "available"
    elif send_result.platform_message_id:
        action.delivery_state = "sent"
    elif send_result.accepted:
        action.delivery_state = "accepted"
    else:
        action.delivery_state = "failed"
    action.save(update_fields=["platform_message_id", "platform_channel", "delivery_state"])
    if _is_cron_create(action) and channel == "app" and not send_result.accepted:
        return False
    # Preserve the gate-routing contract: reaching a selected sender keeps the
    # review pending even when its transport cannot confirm delivery. The
    # independently persisted delivery_state is the truthful narration fact.
    return True


def update_gate_message(action: PendingAction) -> None:
    """Edit/follow-up the confirmation message to show the result."""
    # Re-check at result time: the action may predate an eval-sink backfill or
    # the flag may have changed after the original confirmation was sent.
    if suppresses_real_transport(action.tenant):
        return
    if _is_cron_create(action):
        action.refresh_from_db(fields=["status", "resolution_code"])
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
