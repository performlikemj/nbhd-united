"""Retire exact cron job IDs enumerated from a validated tenant share."""

from __future__ import annotations

import time

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.cron.gateway_client import GatewayError
from apps.cron.share_observer import ShareObservationError
from apps.tenants.models import Tenant

MAX_LIMIT = 100
DEFAULT_LIMIT = 50
REMOVE_THROTTLE_SECONDS = 0.1


class Command(BaseCommand):
    help = "Retire share-enumerated cron jobs by exact gateway job ID."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant UUID")
        parser.add_argument("--name", required=True, help="Exact cron job name")
        parser.add_argument(
            "--bucket",
            required=True,
            choices=("duplicate", "expired"),
            help="Quarantine classification written into each receipt",
        )
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Perform removals. Without this flag, list exact IDs only.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1 or limit > MAX_LIMIT:
            raise CommandError(f"--limit must be between 1 and {MAX_LIMIT}")

        name = options["name"]
        if not name:
            raise CommandError("--name must be non-empty")

        try:
            tenant = Tenant.objects.get(id=options["tenant"])
        except (Tenant.DoesNotExist, ValidationError, ValueError, TypeError):
            raise CommandError(f"Tenant {options['tenant']} not found") from None

        # Local import preserves the gateway/share patch seams used throughout
        # the backend tests and avoids binding network functions before patches.
        from apps.cron.share_observer import observe_share

        try:
            observation = observe_share(tenant)
        except ShareObservationError as exc:
            raise CommandError(f"share observation refused retirement: {exc}") from exc

        matches = [job for job in observation.jobs if job["name"] == name]
        targets = matches[:limit]
        bucket = options["bucket"]

        if not options["confirm"]:
            for job in targets:
                self.stdout.write(
                    f"retire_quarantined: WOULD_REMOVE tenant={tenant.id} bucket={bucket} name={name} jobId={job['id']}"
                )
            self._write_summary(
                tenant=tenant,
                name=name,
                matched=len(matches),
                removed=0,
                failed=0,
            )
            return

        from apps.cron.gateway_client import cron_remove

        removed = 0
        failed = 0
        for index, job in enumerate(targets):
            if index:
                time.sleep(REMOVE_THROTTLE_SECONDS)
            job_id = job["id"]
            try:
                cron_remove(tenant, job_id=job_id)
            except GatewayError as exc:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(
                        "retire_quarantined: FAILED "
                        f"tenant={tenant.id} bucket={bucket} name={name} jobId={job_id} error={exc}"
                    )
                )
                continue

            removed += 1
            self.stdout.write(
                f"retire_quarantined: REMOVED tenant={tenant.id} bucket={bucket} name={name} jobId={job_id}"
            )

        self._write_summary(
            tenant=tenant,
            name=name,
            matched=len(matches),
            removed=removed,
            failed=failed,
        )

    def _write_summary(self, *, tenant, name: str, matched: int, removed: int, failed: int) -> None:
        self.stdout.write(
            "retire_quarantined: "
            f"tenant={tenant.id} name={name} matched={matched} "
            f"removed={removed} failed={failed} remaining={matched - removed}"
        )
