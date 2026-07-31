"""Retire Postgres CronJob ghosts created from unmanaged cron names."""

from __future__ import annotations

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.cron.models import CronJob
from apps.orchestrator.cron_reconcile import _UNMANAGED_PREFIXES
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Retire one tenant's Postgres CronJob rows whose names use an unmanaged gateway-only prefix."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            required=True,
            type=UUID,
            help="Tenant UUID to repair (required; fleet-wide runs are not supported).",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Show matching rows without changing them (the default).",
        )
        mode.add_argument(
            "--confirm",
            action="store_true",
            help="Retire matching rows in Postgres.",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant"]
        confirm = options["confirm"]

        if not Tenant.objects.filter(pk=tenant_id).exists():
            raise CommandError(f"Tenant {tenant_id} not found")

        unmanaged_names = Q()
        for prefix in _UNMANAGED_PREFIXES:
            unmanaged_names |= Q(name__startswith=prefix)

        rows = list(CronJob.objects.filter(tenant_id=tenant_id).filter(unmanaged_names).order_by("name", "id"))
        ids_to_retire = {row.id for row in rows if row.enabled or row.managed}
        already_retired = len(rows) - len(ids_to_retire)

        self.stdout.write("name | id | status | created | action")
        for row in rows:
            status = f"enabled={str(row.enabled).lower()},managed={str(row.managed).lower()}"
            if row.id not in ids_to_retire:
                action = "already retired"
            elif confirm:
                action = "set enabled=false, managed=false"
            else:
                action = "would set enabled=false, managed=false"
            self.stdout.write(f"{row.name} | {row.id} | {status} | {row.created_at.isoformat()} | {action}")

        retired = 0
        if confirm and ids_to_retire:
            retired = (
                CronJob.objects.filter(
                    tenant_id=tenant_id,
                    id__in=ids_to_retire,
                )
                .filter(Q(enabled=True) | Q(managed=True))
                .update(
                    enabled=False,
                    managed=False,
                    updated_at=timezone.now(),
                )
            )

        self.stdout.write(
            f"repair_fuel_cron_rows: tenant={tenant_id} "
            f"matched={len(rows)} retired={retired} "
            f"already_retired={already_retired}"
        )
