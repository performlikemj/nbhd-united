"""Platform-initiated plain-text notifications to a tenant's chat channel.

For one-off system notices that are NOT an LLM turn — e.g. "your model was
switched because the free promo ended". Routes via the canonical, app-first
``resolve_user_channel`` (apps/router/cron_delivery.py): the app when an iOS
device is registered, else Telegram, else LINE, else nowhere. When the resolved
channel is the app there is no chat to push to, so delivery is a
``ProactiveOutbound`` row (which fires the APNs wake-push + writes the ?since=
feed row) — otherwise a platform notice to a token-holding user would silently
vanish once outbound routing became app-first.

The text here is platform-authored and carries no tenant PII, so (unlike the
lesson / gate senders, which echo user content) no rehydration is applied.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def send_system_notification(tenant: Tenant, message: str) -> bool:
    """Send a plain-text notice to the tenant's active channel.

    Returns True if it was handed to a channel API successfully, False if the
    tenant has no linked channel or the send failed.
    """
    channel = _resolve_channel(tenant.user)
    if channel == "app":
        return _send_app(tenant, message)
    if channel == "eval":
        return _send_eval(tenant, message)
    if channel == "telegram":
        return _send_telegram(tenant, message)
    if channel == "line":
        return _send_line(tenant, message)
    logger.info("system_notify: tenant %s has no linked channel; skipped", str(tenant.id)[:8])
    return False


def _resolve_channel(user) -> str | None:
    """App-first channel resolution, shared with the cron / proactive senders.

    Delegates to ``resolve_user_channel`` so system notices route identically:
    app when an iOS device is registered, else Telegram, else LINE, else None.
    ``preferred_channel`` is intentionally not consulted (see resolve_user_channel).
    """
    from apps.router.cron_delivery import resolve_user_channel

    return resolve_user_channel(user)


def _send_app(tenant: Tenant, message: str) -> bool:
    """App-preferred user (iOS device registered): there's no Telegram/LINE chat
    to push to. Recording a ``ProactiveOutbound`` row IS the delivery — the single
    chokepoint that fires the APNs wake-push and writes the ?since= feed row the
    app drains. Without this branch a platform notice (e.g. a model-health switch)
    to a token-holding user would silently vanish now that routing is app-first.
    The text is platform-authored and PII-free, so it stores as-is."""
    from apps.router.proactive_context import record_proactive_outbound

    user = getattr(tenant, "user", None)
    if user is None:
        return False
    row = record_proactive_outbound(
        tenant=tenant,
        channel="app",
        channel_user_id=str(getattr(user, "id", "") or ""),
        message_text=message,
        job_name="_system_notify",
    )
    return row is not None


def _send_eval(tenant: Tenant, message: str) -> bool:
    """Eval-sink tenant: never a real transport. Record the internal ``eval``
    evidence row instead — the recorder suppresses the APNs dispatch for this
    channel and every operational reader excludes the row. This branch keeps a
    platform notice from silently vanishing into the "no linked channel" log
    while honoring the sink contract (recorded, not sent)."""
    from apps.router.proactive_context import record_proactive_outbound

    user = getattr(tenant, "user", None)
    if user is None:
        return False
    row = record_proactive_outbound(
        tenant=tenant,
        channel="eval",
        channel_user_id=str(getattr(user, "id", "") or ""),
        message_text=message,
        job_name="_system_notify",
    )
    return row is not None


def _send_telegram(tenant: Tenant, message: str) -> bool:
    chat_id = getattr(tenant.user, "telegram_chat_id", None)
    if not chat_id:
        return False
    # Plain text (no parse_mode): model ids contain '/', ':' and '-' which would
    # trip Markdown parsing.
    from apps.router.services import send_telegram_message

    try:
        return bool(send_telegram_message(chat_id, message))
    except Exception:
        logger.exception("system_notify: Telegram send failed for tenant %s", str(tenant.id)[:8])
        return False


def _send_line(tenant: Tenant, message: str) -> bool:
    import httpx

    channel_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    line_user_id = getattr(tenant.user, "line_user_id", None)
    if not channel_token or not line_user_id:
        return False
    try:
        resp = httpx.post(
            LINE_PUSH_URL,
            json={"to": line_user_id, "messages": [{"type": "text", "text": message}]},
            headers={"Authorization": f"Bearer {channel_token}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("system_notify: LINE push failed (%s): %s", resp.status_code, resp.text[:200])
        return resp.status_code == 200
    except Exception:
        logger.exception("system_notify: LINE send failed for tenant %s", str(tenant.id)[:8])
        return False
