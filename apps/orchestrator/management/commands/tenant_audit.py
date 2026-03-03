"""Audit tenants — list all tenants and their provisioning state."""
from django.core.management.base import BaseCommand
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "List all tenants with status and container info"

    def handle(self, *args, **options):
        for t in Tenant.objects.all().order_by("created_at"):
            cid = t.container_id or "NONE"
            email = t.user.email if hasattr(t, "user") and t.user else "no-user"
            self.stdout.write(
                f"{t.id} | {t.status:15} | {cid:30} | {t.created_at.date()} | {email}"
            )
        self.stdout.write(f"\nTotal: {Tenant.objects.count()}")
        for status in Tenant.Status.values:
            count = Tenant.objects.filter(status=status).count()
            if count:
                self.stdout.write(f"  {status}: {count}")
