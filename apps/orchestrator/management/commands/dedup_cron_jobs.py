"""Deduplicate cron jobs across all active tenant containers.

For each tenant, lists all cron jobs via the Gateway, groups by name,
and removes duplicates — keeping only the most recently created job
for each name. Supports --dry-run for safe previewing.

Usage:
    python manage.py dedup_cron_jobs              # dry run (default)
    python manage.py dedup_cron_jobs --apply       # actually delete dupes
    python manage.py dedup_cron_jobs --tenant UUID  # single tenant only
"""
from django.core.management.base import BaseCommand

from apps.orchestrator.services import dedup_tenant_cron_jobs
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Remove duplicate cron jobs from tenant OpenClaw containers"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            default=False,
            help="Actually delete duplicates (default is dry-run)",
        )
        parser.add_argument(
            "--tenant",
            type=str,
            default=None,
            help="Only process a specific tenant UUID",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        tenant_filter = options.get("tenant")

        if not apply:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — pass --apply to actually delete duplicates\n"
            ))

        tenants = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
        ).select_related("user")

        if tenant_filter:
            tenants = tenants.filter(id=tenant_filter)

        total_deleted = 0
        total_errors = 0

        for tenant in tenants:
            tenant_id = str(tenant.id)
            display = getattr(tenant.user, "display_name", tenant_id[:8])
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write(f"Tenant: {display} ({tenant_id[:8]}...)")

            result = dedup_tenant_cron_jobs(tenant, dry_run=not apply)

            if result["errors"] and not result["duplicates"]:
                self.stderr.write(self.style.ERROR(
                    f"  Failed to list jobs"
                ))
                total_errors += result["errors"]
                continue

            self.stdout.write(f"  Total unique job names: {result['kept']}")

            if not result["duplicates"]:
                self.stdout.write(self.style.SUCCESS("  No duplicates found."))
                continue

            for dupe in result["duplicates"]:
                self.stdout.write(self.style.WARNING(
                    f"  Duplicate: '{dupe.get('name', '?')}' "
                    f"id={dupe.get('id', '?')[:12]}... "
                    f"(created {dupe.get('createdAt', 'unknown')})"
                ))

            self.stdout.write(f"  Duplicates to remove: {len(result['duplicates'])}")

            if not apply:
                self.stdout.write("  (dry run — no changes made)")
                continue

            total_deleted += result["deleted"]
            total_errors += result["errors"]
            self.stdout.write(self.style.SUCCESS(
                f"  Deleted {result['deleted']}/{len(result['duplicates'])} duplicates"
            ))

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(
            f"Total: {total_deleted} deleted, {total_errors} errors"
        )
        if not apply:
            self.stdout.write(self.style.WARNING(
                "This was a dry run. Pass --apply to execute."
            ))
