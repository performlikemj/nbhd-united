"""Manually deprovision a tenant's OpenClaw instance."""
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.services import deprovision_tenant
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Manually deprovision a tenant's OpenClaw instance"

    def add_arguments(self, parser):
        parser.add_argument("tenant_id", type=str, help="Tenant UUID")

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant {tenant_id} not found")

        self.stdout.write(f"Deprovisioning tenant {tenant_id} ({tenant.user.display_name})...")
        deprovision_tenant(tenant_id)
        self.stdout.write(self.style.SUCCESS("Done!"))
