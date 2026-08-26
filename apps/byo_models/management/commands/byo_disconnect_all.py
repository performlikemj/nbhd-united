"""Disconnect every parked BYO credential and enqueue config reconciliation."""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from apps.byo_models.models import BYOCredential
from apps.byo_models.services import delete_credential
from apps.cron.publish import publish_task

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Disconnect all BYO credentials and reconcile affected tenant configs."

    def handle(self, *args, **options):
        disconnected = 0
        failed = 0
        affected_tenants = {}

        credentials = BYOCredential.objects.select_related("tenant").order_by("tenant_id", "provider")
        for credential in credentials.iterator():
            tenant = credential.tenant
            try:
                delete_credential(credential)
            except Exception:
                failed += 1
                logger.exception("Failed to disconnect a parked BYO credential")
                continue
            disconnected += 1
            affected_tenants[tenant.id] = tenant

        for tenant in affected_tenants.values():
            try:
                tenant.bump_pending_config()
                publish_task("apply_single_tenant_config", str(tenant.id))
            except Exception:
                failed += 1
                logger.exception("Failed to enqueue parked BYO config reconciliation")

        self.stdout.write(f"disconnected={disconnected} failed={failed}")
