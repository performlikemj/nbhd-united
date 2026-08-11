"""Fail-soft, targeted APNs down-channel for queued device commands."""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.common.apns import apns_configured

from .models import DeviceCommand

logger = logging.getLogger(__name__)


def notify_device_command(command: DeviceCommand) -> None:
    """Claim and send one generic hybrid push after command creation commits."""

    if not apns_configured():
        return
    try:
        claimed = DeviceCommand.objects.filter(
            id=command.id,
            notified_at__isnull=True,
        ).update(notified_at=timezone.now())
        if not claimed:
            return
        command = DeviceCommand.objects.select_related("tenant__user").get(id=command.id)

        from apps.router.push_views import _push_to_user_devices

        result = _push_to_user_devices(
            command.tenant.user,
            body="Your assistant has a calendar request — open NBHD",
            thread_id=None,
            collapse_id=f"datebook:{command.id}",
            content_available=True,
            extra={
                "type": "datebook_command",
                "command_id": str(command.id),
            },
            installation_id=command.target_installation_id,
        )
        if result.get("used_fallback"):
            logger.warning(
                "datebook push target installation had no active token; fell back to user tokens "
                "tenant=%s command=%s installation=%s",
                command.tenant_id,
                command.id,
                command.target_installation_id,
            )
    except Exception:
        logger.warning("datebook command push failed (non-fatal) command=%s", command.id, exc_info=True)
