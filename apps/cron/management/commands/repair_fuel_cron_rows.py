"""Retire Postgres CronJob ghosts created from unmanaged cron names."""

from __future__ import annotations

from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.cron.models import CronJob
from apps.orchestrator.cron_reconcile import _UNMANAGED_PREFIXES
from apps.tenants.models import Tenant


def repair_fuel_cron_rows(*, tenant_id: UUID | str, confirm: bool) -> dict:
    """Return and optionally retire one tenant's unmanaged-prefix rows."""
    try:
        tenant_exists = Tenant.objects.filter(pk=tenant_id).exists()
    except (ValidationError, ValueError, TypeError):
        tenant_exists = False
    if not tenant_exists:
        raise CommandError(f"Tenant {tenant_id} not found")

    unmanaged_names = Q()
    for prefix in _UNMANAGED_PREFIXES:
        unmanaged_names |= Q(name__startswith=prefix)

    rows = list(CronJob.objects.filter(tenant_id=tenant_id).filter(unmanaged_names).order_by("name", "id"))
    ids_to_retire = {row.id for row in rows if row.enabled or row.managed}
    already_retired = len(rows) - len(ids_to_retire)

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

    return {
        "matched": len(rows),
        "retired": retired,
        "already_retired": already_retired,
        "rows": [
            {
                "name": row.name,
                "id": str(row.id),
                "enabled": row.enabled,
                "managed": row.managed,
                "created_at": row.created_at.isoformat(),
                "already_retired": row.id not in ids_to_retire,
            }
            for row in rows
        ],
    }


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
        result = repair_fuel_cron_rows(tenant_id=tenant_id, confirm=confirm)

        self.stdout.write("name | id | status | created | action")
        for row in result["rows"]:
            status = f"enabled={str(row['enabled']).lower()},managed={str(row['managed']).lower()}"
            if row["already_retired"]:
                action = "already retired"
            elif confirm:
                action = "set enabled=false, managed=false"
            else:
                action = "would set enabled=false, managed=false"
            self.stdout.write(f"{row['name']} | {row['id']} | {status} | {row['created_at']} | {action}")

        self.stdout.write(
            f"repair_fuel_cron_rows: tenant={tenant_id} "
            f"matched={result['matched']} retired={result['retired']} "
            f"already_retired={result['already_retired']}"
        )
