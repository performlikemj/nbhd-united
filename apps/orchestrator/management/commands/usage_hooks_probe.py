"""Print read-only usage counts for a tenant over a recent window."""

from datetime import timedelta
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone

from apps.billing.models import UsageRecord
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Print UsageRecord counts by event type for a tenant and recent window."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant UUID.")
        parser.add_argument(
            "--since-minutes",
            type=int,
            default=1440,
            help="Lookback window in minutes (default: 1440).",
        )

    def handle(self, *args, **options):
        try:
            tenant_id = UUID(options["tenant"])
        except (TypeError, ValueError) as exc:
            raise CommandError("--tenant must be a valid UUID") from exc

        since_minutes = options["since_minutes"]
        if since_minutes < 1:
            raise CommandError("--since-minutes must be at least 1")
        if not Tenant.objects.filter(id=tenant_id).exists():
            raise CommandError(f"Tenant not found: {tenant_id}")

        since = timezone.now() - timedelta(minutes=since_minutes)
        counts = list(
            UsageRecord.objects.filter(tenant_id=tenant_id, created_at__gte=since)
            .values("event_type")
            .annotate(count=Count("id"))
            .order_by("event_type")
        )

        self.stdout.write(f"Usage records for tenant {tenant_id} over last {since_minutes} minute(s):")
        for row in counts:
            self.stdout.write(f"{row['event_type']}: {row['count']}")
        self.stdout.write(f"total: {sum(row['count'] for row in counts)}")
