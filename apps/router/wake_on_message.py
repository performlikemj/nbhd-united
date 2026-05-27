"""Handle incoming messages for hibernated tenants.

Shared by both Telegram and LINE webhook handlers. Buffers the message,
triggers container wake, and returns a signal telling the caller which
ack message to send (or to stay silent).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.router.models import BufferedMessage
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# If the oldest undelivered buffered message is older than this, the
# previous wake is presumed stalled (silent partial-failure inside
# wake_hibernated_tenant, an out-of-band revision activate, etc).
# We force a fresh wake AND defensively clear hibernated_at so future
# webhooks take the live path even if the wake mechanism itself is
# broken — the user gets a "container restarting" notice instead of
# silently buffering forever.
_WAKE_STALL_THRESHOLD = timedelta(minutes=5)

# Return signals from handle_hibernated_message — drives the caller's ack.
ACK_FRESH = "fresh"  # first msg in a fresh wake → "waking up" ack
ACK_RECONNECT = "reconnect"  # stall-recovery wake → "reconnecting" ack
SILENT = "silent"  # additional msg during in-flight wake → no ack


def handle_hibernated_message(
    tenant: Tenant,
    channel: str,
    payload: dict,
    user_text: str,
) -> str | None:
    """Handle a message for a potentially hibernated tenant.

    Returns one of:
        ACK_FRESH      — first message in a fresh wake; send "waking up" ack
        ACK_RECONNECT  — stall-recovery wake; the prior wake silently failed
                         so the user almost certainly never saw any ack —
                         send a "reconnecting, sorry for the delay" ack
        SILENT         — additional message during an in-flight wake; no ack
        None           — tenant is NOT hibernated; proceed with the live path
    """
    if not tenant.hibernated_at:
        return None

    # Look at existing undelivered buffered messages BEFORE we add this one.
    oldest = BufferedMessage.objects.filter(tenant=tenant, delivered=False).order_by("created_at").first()
    stuck = bool(oldest and (timezone.now() - oldest.created_at) > _WAKE_STALL_THRESHOLD)

    # Buffer the incoming message. ``user_text`` stays untruncated so the
    # hibernated-drain coalesce path can stitch full raw user texts together
    # (apologies + log lines slice locally where they need a short excerpt).
    BufferedMessage.objects.create(
        tenant=tenant,
        channel=channel,
        payload=payload,
        user_text=user_text or "",
    )

    # Update last_message_at even while hibernated
    Tenant.objects.filter(id=tenant.id).update(last_message_at=timezone.now())

    already_waking = bool(oldest) and not stuck

    if not already_waking:
        # Either: (a) first message in queue → fresh wake, or
        #         (b) prior wake stalled → force a fresh wake + clear flag
        from apps.orchestrator.hibernation import wake_hibernated_tenant

        if stuck:
            # Defensively clear hibernated_at so subsequent live messages
            # bypass this hibernation gate even if the fresh wake also
            # silently fails. Buffered messages still flush via QStash
            # retries of deliver_buffered_messages.
            Tenant.objects.filter(id=tenant.id).update(hibernated_at=None)
            tenant.hibernated_at = None
            age_min = (timezone.now() - oldest.created_at).total_seconds() / 60
            logger.warning(
                "wake_on_message: tenant %s — wake stalled (oldest buffered msg %.1f min old); "
                "forcing fresh wake and clearing hibernation flag",
                str(tenant.id)[:8],
                age_min,
            )

        wake_hibernated_tenant(tenant)

        if stuck:
            # The prior wake silently failed, so the user almost certainly
            # never saw the original "waking up" ack. Send a different ack
            # now so they know we noticed and are recovering.
            return ACK_RECONNECT

        logger.info(
            "wake_on_message: tenant %s — first message, waking container",
            str(tenant.id)[:8],
        )
        return ACK_FRESH

    logger.info(
        "wake_on_message: tenant %s — additional message buffered while waking",
        str(tenant.id)[:8],
    )
    return SILENT
