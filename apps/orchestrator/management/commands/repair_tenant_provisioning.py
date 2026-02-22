"""Repair stale tenant provisioning records missing container metadata."""
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.services import repair_stale_tenant_provisioning
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Repair tenants in pending/provisioning/active states that are missing container_id or container_fqdn"

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", type=str, default="", help="Repair a single tenant UUID")
        parser.add_argument("--limit", type=int, default=50, help="Maximum tenants to process")
        parser.add_argument("--dry-run", action="store_true", help="List targets without provisioning")

    def handle(self, *args, **options):
        tenant_id = (options.get("tenant_id") or "").strip()
        limit = options.get("limit")
        dry_run = bool(options.get("dry_run"))

        if tenant_id:
            exists = Tenant.objects.filter(id=tenant_id).exists()
            if not exists:
                raise CommandError(f"Tenant {tenant_id} not found")

        summary = repair_stale_tenant_provisioning(
            tenant_id=tenant_id or None,
            limit=limit,
            dry_run=dry_run,
        )

        self.stdout.write(
            f"evaluated={summary['evaluated']} repaired={summary['repaired']} "
            f"failed={summary['failed']} skipped={summary['skipped']} dry_run={summary['dry_run']}"
        )

        for result in summary["results"]:
            line = (
                f"tenant_id={result['tenant_id']} user_id={result['user_id']} "
                f"status={result['status']} result={result['result']}"
            )
            if result.get("missing"):
                line += f" missing={','.join(result['missing'])}"
            if result.get("error"):
                line += f" error={result['error']}"
            self.stdout.write(line)
