"""Remove duplicate and phantom cron jobs from all active tenant containers.

Keeps the 4 system-seeded jobs (by name) and any unique user-created jobs.
Removes exact duplicates (same name appears more than once).

Usage:
    python manage.py cleanup_cron_jobs --dry-run   # preview what would be removed
    python manage.py cleanup_cron_jobs              # actually remove them
"""
from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand

from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

SYSTEM_JOB_NAMES = {
    "Morning Briefing",
    "Evening Check-in",
    "Week Ahead Review",
    "Background Tasks",
}

MAX_JOBS = 10


class Command(BaseCommand):
    help = "Audit and clean up duplicate/phantom cron jobs across all active tenants."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without removing anything.",
        )
        parser.add_argument(
            "--tenant",
            type=str,
            help="Only process a specific tenant ID.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        tenant_id = options.get("tenant")

        queryset = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
        ).select_related("user")

        if tenant_id:
            queryset = queryset.filter(id=tenant_id)

        tenants = list(queryset)
        self.stdout.write(f"Processing {len(tenants)} active tenant(s)...\n")

        total_removed = 0
        total_errors = 0

        for tenant in tenants:
            try:
                removed, errors = self._process_tenant(tenant, dry_run)
                total_removed += removed
                total_errors += errors
            except Exception:
                total_errors += 1
                logger.exception("Failed to process tenant %s", tenant.id)
                self.stderr.write(f"  ERROR: tenant {tenant.id} — see logs\n")

            # Be gentle with gateway calls
            time.sleep(1)

        action = "Would remove" if dry_run else "Removed"
        self.stdout.write(
            f"\nDone. {action} {total_removed} job(s) across {len(tenants)} tenant(s). "
            f"Errors: {total_errors}\n"
        )

    def _process_tenant(self, tenant: Tenant, dry_run: bool) -> tuple[int, int]:
        """Process a single tenant. Returns (removed_count, error_count)."""
        try:
            list_result = invoke_gateway_tool(
                tenant, "cron.list", {"includeDisabled": True}
            )
        except GatewayError as exc:
            self.stderr.write(f"  SKIP tenant {tenant.id}: cannot list jobs — {exc}\n")
            return 0, 1

        jobs = []
        if isinstance(list_result, dict):
            jobs = list_result.get("jobs", [])
        elif isinstance(list_result, list):
            jobs = list_result

        if not jobs:
            return 0, 0

        self.stdout.write(f"\nTenant {tenant.id}: {len(jobs)} job(s)")

        # Find duplicates: track seen names, mark extras for removal
        seen_names: dict[str, int] = {}
        to_remove: list[dict] = []

        for job in jobs:
            if not isinstance(job, dict):
                continue
            name = job.get("name", "")
            job_id = job.get("jobId", name)

            if name in seen_names:
                seen_names[name] += 1
                to_remove.append({"jobId": job_id, "name": name, "reason": "duplicate"})
                self.stdout.write(f"  DUP: '{name}' (copy #{seen_names[name]})")
            else:
                seen_names[name] = 1

        # Report status
        if not to_remove:
            self.stdout.write(f"  OK — no duplicates found ({len(jobs)} unique jobs)")
            if len(jobs) > MAX_JOBS:
                self.stdout.write(f"  WARNING: {len(jobs)} jobs exceeds cap of {MAX_JOBS}")
        else:
            action = "Would remove" if dry_run else "Removing"
            self.stdout.write(f"  {action} {len(to_remove)} duplicate(s)")

        removed = 0
        errors = 0
        for job in to_remove:
            if dry_run:
                removed += 1
                continue
            try:
                invoke_gateway_tool(tenant, "cron.remove", {"jobId": job["jobId"]})
                removed += 1
                self.stdout.write(f"  REMOVED: '{job['name']}' ({job['reason']})")
            except GatewayError as exc:
                errors += 1
                self.stderr.write(f"  ERROR removing '{job['name']}': {exc}\n")

        return removed, errors
