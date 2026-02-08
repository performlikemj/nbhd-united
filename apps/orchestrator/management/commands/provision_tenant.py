"""Manually provision a tenant's OpenClaw instance."""
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.services import provision_tenant
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Manually provision an OpenClaw instance for a tenant"

    def add_arguments(self, parser):
        parser.add_argument("tenant_id", type=str, help="Tenant UUID")

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant {tenant_id} not found")

        self.stdout.write(f"Provisioning tenant {tenant_id} ({tenant.user.display_name})...")
        provision_tenant(tenant_id)
        tenant.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(
            f"Done! Status: {tenant.status}, Container: {tenant.container_id}"
        ))
