"""Force-apply pending configs to all active tenants immediately.

Bypasses the idle check and QStash requirement of apply_pending_configs cron.
Use when you need configs pushed NOW (e.g., switching to central poller).

Usage: python manage.py force_apply_configs
"""
from django.core.management.base import BaseCommand
from django.db import models

from apps.tenants.models import Tenant
from apps.orchestrator.services import update_tenant_config


class Command(BaseCommand):
    help = "Force-apply pending configs to all active tenants"

    def handle(self, *args, **options):
        tenants = Tenant.objects.filter(
            pending_config_version__gt=models.F("config_version"),
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
        )
        total = tenants.count()
        self.stdout.write(f"Found {total} tenant(s) with pending config updates")

        updated = 0
        failed = 0
        for tenant in tenants:
            try:
                update_tenant_config(str(tenant.id))
                from django.utils import timezone
                Tenant.objects.filter(id=tenant.id).update(
                    config_version=models.F("pending_config_version"),
                    config_refreshed_at=timezone.now(),
                )
                updated += 1
                self.stdout.write(f"  ✅ {tenant.container_id}")
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f"  ❌ {tenant.container_id}: {e}"))

        self.stdout.write(self.style.SUCCESS(
            f"Done: {updated} updated, {failed} failed, {total - updated - failed} skipped"
        ))
