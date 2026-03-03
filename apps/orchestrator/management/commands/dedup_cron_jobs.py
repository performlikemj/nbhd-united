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

from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
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

            # List all jobs
            try:
                list_result = invoke_gateway_tool(
                    tenant, "cron.list", {"includeDisabled": True},
                )
            except GatewayError as exc:
                self.stderr.write(self.style.ERROR(
                    f"  Failed to list jobs: {exc}"
                ))
                total_errors += 1
                continue

            # Unwrap details (same pattern as the fix)
            jobs = []
            if isinstance(list_result, dict):
                inner = list_result.get("details", list_result)
                if isinstance(inner, dict):
                    jobs = inner.get("jobs", [])
                else:
                    jobs = list_result.get("jobs", [])
            elif isinstance(list_result, list):
                jobs = list_result

            self.stdout.write(f"  Total jobs: {len(jobs)}")

            if not jobs:
                self.stdout.write("  No jobs found, skipping.")
                continue

            # Group by name
            by_name: dict[str, list[dict]] = {}
            for job in jobs:
                name = job.get("name", "")
                if not name:
                    continue
                by_name.setdefault(name, []).append(job)

            # Find duplicates
            dupes_to_delete: list[dict] = []
            for name, group in sorted(by_name.items()):
                if len(group) <= 1:
                    continue

                self.stdout.write(self.style.WARNING(
                    f"  '{name}': {len(group)} copies"
                ))

                # Sort by createdAt descending — keep the newest
                group.sort(
                    key=lambda j: j.get("createdAt", j.get("id", "")),
                    reverse=True,
                )
                keeper = group[0]
                self.stdout.write(
                    f"    Keeping: {keeper.get('id', '?')[:12]}... "
                    f"(created {keeper.get('createdAt', 'unknown')})"
                )
                for dupe in group[1:]:
                    self.stdout.write(
                        f"    Deleting: {dupe.get('id', '?')[:12]}... "
                        f"(created {dupe.get('createdAt', 'unknown')})"
                    )
                    dupes_to_delete.append(dupe)

            if not dupes_to_delete:
                self.stdout.write(self.style.SUCCESS("  No duplicates found."))
                continue

            self.stdout.write(f"  Duplicates to remove: {len(dupes_to_delete)}")

            if not apply:
                self.stdout.write("  (dry run — no changes made)")
                continue

            # Delete duplicates
            deleted = 0
            for dupe in dupes_to_delete:
                job_id = dupe.get("id") or dupe.get("name", "")
                try:
                    invoke_gateway_tool(
                        tenant, "cron.remove", {"jobId": job_id},
                    )
                    deleted += 1
                except GatewayError as exc:
                    self.stderr.write(self.style.ERROR(
                        f"  Failed to delete {job_id[:12]}: {exc}"
                    ))
                    total_errors += 1

            total_deleted += deleted
            self.stdout.write(self.style.SUCCESS(
                f"  Deleted {deleted}/{len(dupes_to_delete)} duplicates"
            ))

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(
            f"Total: {total_deleted} deleted, {total_errors} errors"
        )
        if not apply:
            self.stdout.write(self.style.WARNING(
                "This was a dry run. Pass --apply to execute."
            ))
