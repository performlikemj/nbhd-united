"""Push fresh configs to file share for ALL active tenants.

Regenerates openclaw.json from current config_generator and uploads
to each tenant's Azure file share. Use after config_generator changes
to ensure running containers pick up the new config on next restart.

Usage: python manage.py push_all_configs
"""
from django.core.management.base import BaseCommand

from apps.tenants.models import Tenant
from apps.orchestrator.services import update_tenant_config


class Command(BaseCommand):
    help = "Regenerate and push configs to file share for all active tenants"

    def handle(self, *args, **options):
        tenants = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
        ).exclude(container_id="")

        total = tenants.count()
        self.stdout.write(f"Pushing configs for {total} active tenant(s)...")

        updated = 0
        failed = 0
        for tenant in tenants:
            try:
                update_tenant_config(str(tenant.id))
                updated += 1
                self.stdout.write(f"  ✅ {tenant.container_id}")
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(
                    f"  ❌ {tenant.container_id}: {e}"
                ))

        self.stdout.write(self.style.SUCCESS(
            f"Done: {updated} pushed, {failed} failed"
        ))
