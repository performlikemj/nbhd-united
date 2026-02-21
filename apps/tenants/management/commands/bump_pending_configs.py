"""Set pending config versions to current+1 for active tenants with containers."""

from django.core.management.base import BaseCommand
from django.db.models import F

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Bump pending_config_version to config_version + 1 for active tenants with containers"

    def handle(self, *args, **options):
        count = Tenant.objects.filter(
            status="active",
            container_id__gt="",
        ).update(pending_config_version=F("config_version") + 1)

        self.stdout.write(
            self.style.SUCCESS(
                f"Bumped pending_config_version for {count} tenant(s)."
            )
        )
