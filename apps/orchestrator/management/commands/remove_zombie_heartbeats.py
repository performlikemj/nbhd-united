"""Remove zombie Heartbeat Check-in cron jobs from tenants with heartbeat disabled.

When heartbeat_enabled is turned off on a tenant, the existing cron job
in OpenClaw is not removed — it keeps firing and failing every hour.
This command finds and removes those zombie jobs.

Usage:
    python manage.py remove_zombie_heartbeats --dry-run
    python manage.py remove_zombie_heartbeats
"""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand

from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

HEARTBEAT_JOB_NAME = "Heartbeat Check-in"


class Command(BaseCommand):
    help = "Remove zombie Heartbeat Check-in cron jobs from tenants with heartbeat disabled."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without removing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        tenants = list(
            Tenant.objects.filter(
                status=Tenant.Status.ACTIVE,
                container_id__gt="",
                heartbeat_enabled=False,
            ).select_related("user")
        )
        self.stdout.write(f"Found {len(tenants)} active tenant(s) with heartbeat disabled.\n")

        removed = 0
        skipped = 0
        errors = 0

        for tenant in tenants:
            try:
                list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
            except GatewayError as exc:
                self.stderr.write(f"  SKIP {tenant.id}: cannot list jobs — {exc}\n")
                errors += 1
                time.sleep(1)
                continue

            jobs = []
            if isinstance(list_result, dict):
                inner = list_result.get("details", list_result)
                if isinstance(inner, dict):
                    jobs = inner.get("jobs", [])
                else:
                    jobs = list_result.get("jobs", [])
            elif isinstance(list_result, list):
                jobs = list_result

            heartbeat_job = None
            for job in jobs:
                if isinstance(job, dict) and job.get("name") == HEARTBEAT_JOB_NAME:
                    heartbeat_job = job
                    break

            if not heartbeat_job:
                self.stdout.write(f"  {tenant.id}: no heartbeat job found — OK")
                skipped += 1
                time.sleep(0.5)
                continue

            job_id = heartbeat_job.get("id") or heartbeat_job.get("jobId", HEARTBEAT_JOB_NAME)

            if dry_run:
                self.stdout.write(f"  {tenant.id}: WOULD REMOVE '{HEARTBEAT_JOB_NAME}' (id={job_id})")
                removed += 1
            else:
                try:
                    invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
                    self.stdout.write(f"  {tenant.id}: REMOVED '{HEARTBEAT_JOB_NAME}'")
                    removed += 1
                except GatewayError as exc:
                    self.stderr.write(f"  {tenant.id}: ERROR removing heartbeat — {exc}\n")
                    errors += 1

            time.sleep(1)

        action = "Would remove" if dry_run else "Removed"
        self.stdout.write(f"\nDone. {action} {removed} zombie heartbeat(s). Skipped: {skipped}. Errors: {errors}\n")
