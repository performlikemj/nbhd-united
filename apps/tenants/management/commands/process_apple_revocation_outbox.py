"""Republish stale Apple revocation rows whose original QStash publish failed."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tenants.apple_models import AppleRevocationOutbox

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Republish unrevoked Apple outbox rows older than one hour."

    def handle(self, *args, **options):
        from apps.cron.publish import publish_task

        cutoff = timezone.now() - timedelta(hours=1)
        rows = (
            AppleRevocationOutbox.objects.filter(
                revoked_at__isnull=True,
                created_at__lte=cutoff,
            )
            .exclude(last_error__startswith="terminal:")
            .values_list("id", flat=True)
        )
        queued = 0
        failed = 0
        for row_id in rows.iterator():
            outbox_id = str(row_id)
            try:
                publish_task(
                    "revoke_apple_token",
                    outbox_id,
                    idempotency_key=f"apple-revoke-{outbox_id}",
                )
                queued += 1
            except Exception:
                failed += 1
                logger.warning(
                    "auth.apple.revocation.republish_failed outbox_id=%s",
                    outbox_id,
                    exc_info=True,
                )
        self.stdout.write(f"queued={queued} failed={failed}")
