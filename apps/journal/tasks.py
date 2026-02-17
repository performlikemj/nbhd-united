"""Tasks for journal memory sync (executed via QStash, not Celery)."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def sync_documents_to_workspace(tenant_id: str):
    """Mirror tenant's DB documents to their Azure File Share workspace.

    Renders recent documents as markdown files and uploads them so
    OpenClaw's built-in ``memory_search`` (vector search) can index them
    alongside regular workspace memory files.

    Files are written to ``memory/journal/<kind>/<slug>.md`` within the
    tenant's workspace file share.
    """
    from apps.tenants.models import Tenant

    try:
        tenant = Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist:
        logger.warning(
            "sync_documents_to_workspace: tenant %s not found", tenant_id
        )
        return None

    if tenant.status != Tenant.Status.ACTIVE:
        logger.info(
            "sync_documents_to_workspace: tenant %s not active (status=%s), skipping",
            tenant_id,
            tenant.status,
        )
        return None

    from apps.orchestrator.memory_sync import (
        render_memory_files,
        upload_memory_files_to_share,
    )

    try:
        files = render_memory_files(tenant)
        if not files:
            logger.info(
                "sync_documents_to_workspace: tenant=%s no documents to sync",
                tenant_id,
            )
            return {"synced": 0, "total": 0}

        written = upload_memory_files_to_share(str(tenant.id), files)
        return {"synced": written, "total": len(files)}

    except Exception:
        logger.exception(
            "sync_documents_to_workspace failed for tenant %s", tenant_id
        )
        raise
