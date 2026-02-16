"""Post-save signals for journal models."""
from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.journal.models import Document

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Document)
def queue_memory_sync_on_document_save(sender, instance, **kwargs):
    """Queue a workspace memory sync whenever a Document is saved."""
    from apps.journal.tasks import sync_documents_to_workspace

    tenant_id = str(instance.tenant_id)
    try:
        sync_documents_to_workspace.delay(tenant_id)
    except Exception:
        logger.warning(
            "Failed to queue memory sync for tenant %s",
            tenant_id,
            exc_info=True,
        )
