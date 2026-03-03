"""Update system cron job prompts for all active tenants.

Patches existing cron jobs with current prompts from config_generator.
Does not delete or recreate jobs — only updates the payload message.

Usage: python manage.py update_cron_prompts
"""
from django.core.management.base import BaseCommand

from apps.orchestrator.services import update_system_cron_prompts
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Update system cron prompts for all active tenants"

    def handle(self, *args, **options):
        tenants = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
        ).exclude(container_id="")

        total = tenants.count()
        self.stdout.write(f"Updating cron prompts for {total} tenant(s)...")

        updated = 0
        failed = 0
        for tenant in tenants:
            try:
                result = update_system_cron_prompts(tenant)
                updated += result["updated"]
                failed += result["errors"]
                self.stdout.write(f"  ✅ {str(tenant.id)[:8]}: {result['updated']} updated")
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f"  ❌ {str(tenant.id)[:8]}: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Done: {updated} prompts updated, {failed} errors"))
