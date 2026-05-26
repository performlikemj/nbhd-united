"""Background tasks for the router app."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from django.conf import settings

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

MEDIA_DIR = "workspace/media/inbound"
MAX_AGE = timedelta(hours=24)


def cleanup_inbound_media_task() -> None:
    """Delete inbound media files older than 24 hours from all tenant file shares.

    Called via QStash cron schedule (daily).
    """
    from azure.storage.fileshare import ShareDirectoryClient

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        logger.warning("AZURE_STORAGE_ACCOUNT_NAME not configured, skipping media cleanup")
        return

    from apps.orchestrator.azure_client import _is_mock, get_storage_client

    if _is_mock():
        logger.info("[MOCK] Would clean up inbound media for all tenants")
        return

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account_name,
    )
    account_key = keys.keys[0].value

    cutoff = datetime.now(UTC) - MAX_AGE
    total_deleted = 0

    tenants = Tenant.objects.filter(status=Tenant.Status.ACTIVE).exclude(container_id="")
    for tenant in tenants:
        share_name = f"ws-{str(tenant.id)[:20]}"
        try:
            dir_client = ShareDirectoryClient(
                account_url=f"https://{account_name}.file.core.windows.net",
                share_name=share_name,
                directory_path=MEDIA_DIR,
                credential=account_key,
            )
            files = list(dir_client.list_directories_and_files())
        except Exception:
            # Directory doesn't exist yet — no media uploaded for this tenant
            continue

        for item in files:
            if item.get("is_directory"):
                continue
            # Check last modified time
            try:
                file_props = dir_client.get_file_client(item["name"]).get_file_properties()
                last_modified = file_props.last_modified
                if last_modified and last_modified < cutoff:
                    dir_client.get_file_client(item["name"]).delete_file()
                    total_deleted += 1
            except Exception:
                logger.debug("Failed to check/delete %s in %s", item["name"], share_name)

    logger.info("Media cleanup complete: deleted %d files across %d tenants", total_deleted, tenants.count())


def poll_line_quota_task() -> dict:
    """Daily poll of the LINE Push monthly quota.

    Refreshes :class:`apps.router.models.LineQuotaState` from the LINE
    Messaging API and, on any threshold crossing, enqueues
    ``dispatch_line_quota_handler`` so the fan-out (emails + channel
    flips) happens out-of-band. The handler is idempotent, so it's
    fine for both this task and the 429 tripwire to enqueue it for
    the same event.

    Cadence: once daily (registered via ``register_system_crons``).
    """
    from apps.cron.publish import publish_task
    from apps.router.line_quota import refresh_quota_state

    result = refresh_quota_state()

    if result.transitions:
        try:
            publish_task("dispatch_line_quota_handler")
        except Exception:
            logger.exception("poll_line_quota: failed to enqueue handler dispatch")

    return {
        "polled": result.polled,
        "limit": result.limit,
        "used": result.used,
        "transitions": list(result.transitions),
    }


def dispatch_line_quota_handler_task() -> dict:
    """Run the LINE quota state-transition handlers (pre-warn email,
    exhaustion fan-out, recovery fan-out). Idempotent — each handler
    short-circuits if its event has already been notified.

    Enqueued by:
      - ``poll_line_quota_task`` when the daily poll detects a transition
      - The 429 tripwire in ``apps.router.line_webhook._maybe_trip_monthly_quota``
        immediately on exhaustion (so users don't wait up to 24h for
        the email after the cap is hit mid-day).
    """
    from apps.router.line_quota_handlers import dispatch_for_current_state

    return dispatch_for_current_state()
