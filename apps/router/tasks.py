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
