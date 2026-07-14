"""Platform-initiated plain-text notifications to a tenant's chat channel.

For one-off system notices that are NOT an LLM turn — e.g. "your model was
switched because the free promo ended". Channel selection goes through the
canonical :func:`apps.router.cron_delivery.resolve_user_channel` (no local copy).

Telegram/LINE are being decommissioned (Phase 1 — see
``CONTINUITY_channel_decommission.md``): the resolver now returns only ``"app"``
or ``None``, so these Telegram/LINE senders are unreachable and system notices
are skipped for iOS-only tenants until a Phase-3 rewrite adds an app path here.
The senders are left in place so a single resolver revert restores delivery.

The text here is platform-authored and carries no tenant PII, so (unlike the
lesson / gate senders, which echo user content) no rehydration is applied.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.router.cron_delivery import resolve_user_channel
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def send_system_notification(tenant: Tenant, message: str) -> bool:
    """Send a plain-text notice to the tenant's active channel.

    Returns True if it was handed to a channel API successfully, False if the
    tenant has no linked channel or the send failed.
    """
    channel = resolve_user_channel(tenant.user)
    if channel == "telegram":
        return _send_telegram(tenant, message)
    if channel == "line":
        return _send_line(tenant, message)
    logger.info("system_notify: tenant %s has no telegram/line channel; skipped", str(tenant.id)[:8])
    return False


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
