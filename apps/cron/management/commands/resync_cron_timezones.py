"""Resync system cron jobs to each tenant's configured timezone.

Deletes and recreates the canonical system crons (Morning Briefing,
Evening Check-in, Week Ahead Review, Background Tasks) for every active
tenant whose container is running.  Safe to run multiple times.

Usage:
    python manage.py resync_cron_timezones
    python manage.py resync_cron_timezones --dry-run
    python manage.py resync_cron_timezones --tenant <uuid>
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from apps.orchestrator.config_generator import build_cron_seed_jobs
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Resync system cron jobs to each active tenant's configured timezone."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be done without making changes.",
        )
        parser.add_argument(
            "--tenant",
            metavar="UUID",
            help="Only process a single tenant (useful for testing).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        tenant_filter = options.get("tenant")

        qs = Tenant.objects.filter(status=Tenant.Status.ACTIVE).select_related("user")
        if tenant_filter:
            qs = qs.filter(id=tenant_filter)

        tenants = list(qs)
        self.stdout.write(
            f"Processing {len(tenants)} tenant(s)"
            + (" [DRY RUN]" if dry_run else "")
        )

        total_ok = total_skip = total_err = 0

        for tenant in tenants:
            if not tenant.container_fqdn:
                self.stdout.write(f"  skip {tenant.id} — no container")
                total_skip += 1
                continue

            user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
            seed_jobs = build_cron_seed_jobs(tenant)
            seed_names = {j["name"] for j in seed_jobs}

            self.stdout.write(f"  tenant {tenant.id}  tz={user_tz}")

            if dry_run:
                for name in seed_names:
                    self.stdout.write(f"    would delete+recreate: {name}")
                total_ok += 1
                continue

            try:
                # Fetch existing jobs
                list_result = invoke_gateway_tool(
                    tenant, "cron.list", {"includeDisabled": True}
                )
                jobs = []
                if isinstance(list_result, dict):
                    jobs = list_result.get("jobs", [])
                elif isinstance(list_result, list):
                    jobs = list_result

                # Delete system crons
                deleted = 0
                for job in jobs:
                    if job.get("name", "") in seed_names:
                        job_id = job.get("jobId") or job.get("name")
                        try:
                            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
                            deleted += 1
                        except GatewayError as e:
                            self.stderr.write(
                                f"    warn: could not delete {job_id}: {e}"
                            )

                # Recreate with correct timezone
                created = 0
                for job in seed_jobs:
                    try:
                        invoke_gateway_tool(tenant, "cron.add", {"job": job})
                        created += 1
                    except GatewayError as e:
                        self.stderr.write(
                            f"    warn: could not create {job['name']}: {e}"
                        )

                self.stdout.write(
                    f"    deleted={deleted} recreated={created}"
                )
                total_ok += 1

            except Exception as e:
                self.stderr.write(f"    ERROR: {e}")
                logger.exception("resync_cron_timezones failed for tenant %s", tenant.id)
                total_err += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone — ok={total_ok} skipped={total_skip} errors={total_err}"
            )
        )
