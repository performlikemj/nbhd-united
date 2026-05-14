"""Refresh postgres CronJob rows from the current seed for all active tenants.

For postgres-canonical tenants, this updates the rows in place; the
post_save signal triggers ``regenerate_tenant_crons`` which pushes the
new state to each tenant's OpenClaw runtime. Use this after a seed
prompt change (e.g. updating Morning Briefing template) to roll the new
prompt out fleet-wide.

Usage: python manage.py update_cron_prompts
"""

from django.core.management.base import BaseCommand

from apps.orchestrator.services import refresh_system_cron_rows_from_seed
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Refresh system cron rows from seed for all active tenants"

    def handle(self, *args, **options):
        tenants = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
        ).exclude(container_id="")

        total = tenants.count()
        self.stdout.write(f"Refreshing system cron rows for {total} tenant(s)...")

        updated = 0
        failed = 0
        for tenant in tenants:
            try:
                result = refresh_system_cron_rows_from_seed(tenant)
                updated += result["created"] + result["updated"]
                self.stdout.write(
                    f"  ✅ {str(tenant.id)[:8]}: "
                    f"created={result['created']} updated={result['updated']} "
                    f"preserved_custom={result['preserved_custom']}"
                )
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f"  ❌ {str(tenant.id)[:8]}: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Done: {updated} rows touched, {failed} errors"))
