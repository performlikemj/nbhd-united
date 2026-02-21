from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Print current local timestamp for a tenant's timezone"

    def add_arguments(self, parser):
        parser.add_argument("tenant_id", type=str, help="Tenant UUID")

    def _tenant_timezone(self, tenant: Tenant) -> str:
        candidate = str(getattr(tenant.user, "timezone", "") or "UTC")
        try:
            ZoneInfo(candidate)
            return candidate
        except ZoneInfoNotFoundError:
            return "UTC"

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        try:
            tenant = Tenant.objects.select_related("user").get(pk=tenant_id)
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Tenant not found: {tenant_id}") from exc

        tz_name = self._tenant_timezone(tenant)
        now = timezone.now().astimezone(ZoneInfo(tz_name))

        self.stdout.write(f"tenant_id={tenant.id}")
        self.stdout.write(f"timezone={tz_name}")
        self.stdout.write(f"utc_time={timezone.now().isoformat()}")
        self.stdout.write(f"local_time={now.isoformat()}")
        self.stdout.write(f"local_date={now.date()}")
        self.stdout.write(f"local_time_hhmm={now.strftime('%H:%M')}")
        self.stdout.write("ok")
