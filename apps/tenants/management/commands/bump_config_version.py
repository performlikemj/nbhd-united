"""Increment tenant pending configuration versions.

Usage:
    python manage.py bump_config_version
    python manage.py bump_config_version --tenant <tenant-uuid>
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Increment pending_config_version for tenants"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            type=str,
            default=None,
            help="Target a single tenant by UUID",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant"]

        if tenant_id:
            qs = Tenant.objects.filter(id=tenant_id)
            if not qs.exists():
                raise CommandError(f"Tenant {tenant_id} not found")
            tenants = qs
        else:
            tenants = Tenant.objects.filter(status=Tenant.Status.ACTIVE)

        if not tenants.exists():
            self.stdout.write(self.style.WARNING("No matching tenants found."))
            return

        count = tenants.update(pending_config_version=F("pending_config_version") + 1)
        self.stdout.write(self.style.SUCCESS(f"Bumped pending_config_version for {count} tenant(s)."))
