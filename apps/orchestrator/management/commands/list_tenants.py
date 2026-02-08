"""List all tenants and their status."""
from django.core.management.base import BaseCommand

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "List all tenants with their status and container info"

    def add_arguments(self, parser):
        parser.add_argument("--status", type=str, help="Filter by status")

    def handle(self, *args, **options):
        qs = Tenant.objects.select_related("user").all()
        if options["status"]:
            qs = qs.filter(status=options["status"])

        if not qs.exists():
            self.stdout.write("No tenants found.")
            return

        for t in qs:
            self.stdout.write(
                f"{t.id}  {t.user.display_name:<20}  "
                f"{t.status:<15}  {t.model_tier:<10}  "
                f"{t.container_id or '(none)':<30}  "
                f"msgs={t.messages_this_month}"
            )
