"""Post-save signals for journal models."""

from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.journal.models import Document, PendingExtraction

logger = logging.getLogger(__name__)

_EXTRACTION_KIND_MAP = {
    Document.Kind.TASKS: PendingExtraction.Kind.TASK,
    Document.Kind.GOAL: PendingExtraction.Kind.GOAL,
}


def _qstash_configured() -> bool:
    return bool(getattr(settings, "QSTASH_TOKEN", "")) and bool(getattr(settings, "API_BASE_URL", ""))


@receiver(post_save, sender=Document)
def queue_memory_sync_on_document_save(sender, instance, **kwargs):
    """Queue a workspace memory sync whenever a Document is saved.

    In production (QStash configured), the actual publish runs in a daemon
    thread *after* the document save commits — so the runtime endpoint that
    triggered this save returns immediately rather than blocking on the
    QStash HTTP round-trip. This is the root-cause fix for
    `nbhd_daily_note_set_section` 20s timeouts seen in canary logs.

    In dev/test (no QStash), falls back to synchronous execution so test
    assertions about task side effects still work.
    """
    tenant_id = str(instance.tenant_id)

    def _publish() -> None:
        from apps.cron.publish import publish_task

        try:
            publish_task("sync_documents_to_workspace", tenant_id)
        except Exception:
            logger.warning(
                "Failed to queue memory sync for tenant %s",
                tenant_id,
                exc_info=True,
            )

    if _qstash_configured():
        # transaction.on_commit guarantees we only publish after the save
        # is durable. The thread keeps the request thread unblocked.
        transaction.on_commit(lambda: threading.Thread(target=_publish, daemon=True).start())
    else:
        # Synchronous path preserved so tests that assert on side effects
        # of the synchronous-fallback publish_task continue to work.
        transaction.on_commit(_publish)


@receiver(post_save, sender=Document)
def auto_resolve_pending_extractions(sender, instance, **kwargs):
    """When a Tasks or Goals document is saved, auto-approve any pending
    extractions whose text appears in the document content."""
    extraction_kind = _EXTRACTION_KIND_MAP.get(instance.kind)
    if not extraction_kind:
        return

    markdown_lower = (instance.markdown or "").lower()
    if not markdown_lower:
        return

    pending = PendingExtraction.objects.filter(
        tenant=instance.tenant,
        kind=extraction_kind,
        status=PendingExtraction.Status.PENDING,
    )

    to_resolve = []
    for p in pending:
        extraction_text = p.text.lower().strip()
        if not extraction_text:
            continue
        # Direct substring match
        if extraction_text in markdown_lower:
            to_resolve.append(p.id)
            continue
        # Fallback: significant word overlap (>= 70%)
        words = [w for w in extraction_text.split() if len(w) > 3]
        if words and sum(1 for w in words if w in markdown_lower) / len(words) >= 0.7:
            to_resolve.append(p.id)

    if to_resolve:
        count = PendingExtraction.objects.filter(id__in=to_resolve).update(
            status=PendingExtraction.Status.APPROVED,
            resolved_at=timezone.now(),
        )
        logger.info(
            "Auto-resolved %d pending extractions for tenant %s",
            count,
            str(instance.tenant_id)[:8],
        )
