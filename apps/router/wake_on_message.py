"""Handle incoming messages for hibernated tenants.

Shared by both Telegram and LINE webhook handlers. Buffers the message,
triggers container wake, and returns whether to send the "waking up" ack.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.router.models import BufferedMessage
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def handle_hibernated_message(
    tenant: Tenant,
    channel: str,
    payload: dict,
    user_text: str,
) -> bool | None:
    """Handle a message for a potentially hibernated tenant.

    Returns:
        True  — first message during hibernation; caller should send ack
        False — subsequent message; caller should stay silent (already acking)
        None  — tenant is NOT hibernated; caller should proceed normally
    """
    if not tenant.hibernated_at:
        return None

    # Check if we're already in the process of waking
    already_waking = BufferedMessage.objects.filter(
        tenant=tenant, delivered=False,
    ).exists()

    # Buffer the incoming message
    BufferedMessage.objects.create(
        tenant=tenant,
        channel=channel,
        payload=payload,
        user_text=(user_text or "")[:200],
    )

    # Update last_message_at even while hibernated
    Tenant.objects.filter(id=tenant.id).update(last_message_at=timezone.now())

    if not already_waking:
        # First message — trigger wake
        from apps.orchestrator.hibernation import wake_hibernated_tenant

        wake_hibernated_tenant(tenant)
        logger.info(
            "wake_on_message: tenant %s — first message, waking container",
            str(tenant.id)[:8],
        )
        return True

    logger.info(
        "wake_on_message: tenant %s — additional message buffered while waking",
        str(tenant.id)[:8],
    )
    return False
