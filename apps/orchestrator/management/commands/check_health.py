"""Check health of all active tenant containers."""
from django.core.management.base import BaseCommand

from apps.orchestrator.services import check_tenant_health
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Check health of all active OpenClaw instances"

    def handle(self, *args, **options):
        tenants = Tenant.objects.filter(status=Tenant.Status.ACTIVE)

        if not tenants.exists():
            self.stdout.write("No active tenants.")
            return

        for tenant in tenants:
            result = check_tenant_health(str(tenant.id))
            status_icon = "✅" if result["healthy"] else "❌"
            self.stdout.write(
                f"{status_icon}  {tenant.id}  {tenant.user.display_name:<20}  "
                f"{result.get('container', '(none)')}"
            )
