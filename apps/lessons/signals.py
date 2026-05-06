"""Post-save signals for lesson models."""

from __future__ import annotations

import logging
import threading

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.lessons.models import Lesson

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Lesson)
def refresh_user_md_on_lesson_save(sender, instance, **kwargs):
    """Refresh ``workspace/USER.md`` whenever an approved lesson is saved.

    Only the three most recent approved lessons appear in the envelope's
    ``Recent lessons`` section, so most lesson saves are a no-op for the
    envelope. We still debounce-push on every approved save to keep the
    section fresh — the leading-edge cache key in ``push_user_md`` collapses
    bursts into one write.
    """
    if instance.status != "approved":
        return

    tenant_id = str(instance.tenant_id)

    def _push() -> None:
        from apps.orchestrator.workspace_envelope import push_user_md

        try:
            push_user_md(tenant_id)
        except Exception:
            logger.warning(
                "USER.md refresh after Lesson save failed for tenant %s",
                tenant_id,
                exc_info=True,
            )

    transaction.on_commit(lambda: threading.Thread(target=_push, daemon=True).start())
