"""Scale suspended tenant containers to zero replicas.

Finds all tenants with status=SUSPENDED that still have a container_id
and scales them to min=0, max=0 replicas. This stops Azure resource
costs while keeping the container available for fast reactivation.

Usage: python manage.py hibernate_suspended [--dry-run]
"""

from django.core.management.base import BaseCommand

from apps.orchestrator.azure_client import hibernate_container_app
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Hibernate suspended tenant containers (deactivate revisions)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List targets without hibernating",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        tenants = Tenant.objects.filter(
            status=Tenant.Status.SUSPENDED,
        ).exclude(container_id="")

        total = tenants.count()
        self.stdout.write(f"Found {total} suspended tenant(s) with containers")

        hibernated = 0
        failed = 0
        for tenant in tenants:
            if dry_run:
                self.stdout.write(f"  [dry-run] would hibernate: {tenant.container_id} ({tenant.user.email})")
                continue
            try:
                hibernate_container_app(tenant.container_id)
                hibernated += 1
                self.stdout.write(self.style.SUCCESS(f"  ✅ {tenant.container_id} ({tenant.user.email})"))
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f"  ❌ {tenant.container_id}: {e}"))

        if dry_run:
            self.stdout.write(f"\nDry run: {total} would be hibernated")
        else:
            self.stdout.write(self.style.SUCCESS(f"\nDone: {hibernated} hibernated, {failed} failed"))
