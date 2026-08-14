"""Fail-soft, targeted APNs down-channel for queued device commands."""

from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from apps.common.apns import apns_configured
from apps.tenants.models import Tenant

from .models import DatebookGateway, DeviceCommand

logger = logging.getLogger(__name__)


def _dispatch_off_request_path(target, *args) -> None:
    """Run a fail-soft datebook notification without holding the HTTP response.

    Pushes are invalidations only; the database remains the source of truth. Tests
    can force the established synchronous seam with
    ``NBHD_DISABLE_BACKGROUND_THREADS``.
    """

    if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
        target(*args)
        return

    def _threaded() -> None:
        close_old_connections()
        try:
            target(*args)
        except Exception:
            logger.warning("datebook background notification failed (non-fatal)", exc_info=True)
        finally:
            close_old_connections()

    try:
        threading.Thread(target=_threaded, daemon=True, name="datebook-notify").start()
    except Exception:
        logger.warning("datebook background notification could not start (non-fatal)", exc_info=True)


def notify_datebook_gate_changed(tenant_id) -> None:
    """Send one PII-free invalidation to the active gateway installation only."""

    if not apns_configured():
        return
    try:
        gateway = (
            DatebookGateway.objects.select_related("tenant__user")
            .filter(tenant_id=tenant_id, status=DatebookGateway.Status.ACTIVE)
            .first()
        )
        if gateway is None or gateway.tenant.status != Tenant.Status.ACTIVE:
            return

        from apps.router.push_views import _push_to_user_devices

        _push_to_user_devices(
            gateway.tenant.user,
            body="Your Calendar & Reminders approvals changed — open NBHD",
            thread_id=None,
            collapse_id=f"datebook-gate-changed:{tenant_id}",
            content_available=True,
            extra={"type": "datebook_gate_changed"},
            installation_id=gateway.installation_id,
            fallback_to_all=False,
        )
    except Exception:
        logger.warning("datebook gate-changed push failed (non-fatal) tenant=%s", tenant_id, exc_info=True)


def notify_device_command(command_id) -> None:
    """Claim and send one generic hybrid push after command creation commits."""

    if not apns_configured():
        return
    command_id = getattr(command_id, "id", command_id)
    try:
        claimed = DeviceCommand.objects.filter(
            id=command_id,
            notified_at__isnull=True,
        ).update(notified_at=timezone.now())
        if not claimed:
            return
        command = DeviceCommand.objects.select_related("tenant__user").get(id=command_id)

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
        logger.warning("datebook command push failed (non-fatal) command=%s", command_id, exc_info=True)


def dispatch_datebook_gate_changed(tenant_id) -> None:
    _dispatch_off_request_path(notify_datebook_gate_changed, tenant_id)


def dispatch_device_command(command: DeviceCommand) -> None:
    _dispatch_off_request_path(notify_device_command, command.id)
