"""Regenerate OpenClaw configs for all active tenants (or a single tenant)."""
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.services import update_tenant_config
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Regenerate and push OpenClaw configs for all active tenants"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=str,
            default=None,
            help="Target a single tenant by UUID",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List tenants that would be updated without making changes",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        dry_run = options["dry_run"]

        if tenant_id:
            try:
                tenants = Tenant.objects.filter(id=tenant_id)
                if not tenants.exists():
                    raise Tenant.DoesNotExist
            except (Tenant.DoesNotExist, ValueError):
                raise CommandError(f"Tenant {tenant_id} not found")
        else:
            tenants = Tenant.objects.filter(
                status=Tenant.Status.ACTIVE,
            ).exclude(container_id="")

        if not tenants.exists():
            self.stdout.write(self.style.WARNING("No matching tenants found."))
            return

        self.stdout.write(f"Found {tenants.count()} tenant(s).")

        for tenant in tenants.select_related("user"):
            label = f"{tenant.id} ({tenant.user.display_name})"
            if dry_run:
                self.stdout.write(f"  [dry-run] Would update: {label}")
                continue

            self.stdout.write(f"  Updating: {label} ...")
            try:
                update_tenant_config(str(tenant.id))
                self.stdout.write(self.style.SUCCESS(f"    OK"))
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"    FAILED: {exc}"))

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes made."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
