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


def retire_quarantined(
    *,
    tenant_id,
    name: str,
    bucket: str,
    limit: int = DEFAULT_LIMIT,
    confirm: bool = False,
) -> dict:
    """Return and optionally remove exact matching gateway cron IDs."""
    if limit < 1 or limit > MAX_LIMIT:
        raise CommandError(f"--limit must be between 1 and {MAX_LIMIT}")
    if not name:
        raise CommandError("--name must be non-empty")
    if bucket not in {"duplicate", "expired"}:
        raise CommandError("--bucket must be duplicate or expired")

    try:
        tenant = Tenant.objects.get(id=tenant_id)
    except (Tenant.DoesNotExist, ValidationError, ValueError, TypeError):
        raise CommandError(f"Tenant {tenant_id} not found") from None

    # Local import preserves the gateway/share patch seams used throughout
    # the backend tests and avoids binding network functions before patches.
    from apps.cron.share_observer import observe_share

    try:
        observation = observe_share(tenant)
    except ShareObservationError as exc:
        raise CommandError(f"share observation refused retirement: {exc}") from exc

    matches = [job for job in observation.jobs if job["name"] == name]
    targets = matches[:limit]
    attempts = []
    removed_ids = []

    if not confirm:
        attempts = [{"job_id": job["id"], "status": "would_remove"} for job in targets]
    else:
        from apps.cron.gateway_client import cron_remove

        for index, job in enumerate(targets):
            if index:
                time.sleep(REMOVE_THROTTLE_SECONDS)
            job_id = job["id"]
            try:
                cron_remove(tenant, job_id=job_id)
            except GatewayError as exc:
                attempts.append(
                    {
                        "job_id": job_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                continue

            removed_ids.append(job_id)
            attempts.append({"job_id": job_id, "status": "removed"})

    failed = sum(attempt["status"] == "failed" for attempt in attempts)
    return {
        "matched": len(matches),
        "removed": len(removed_ids),
        "failed": failed,
        "remaining": len(matches) - len(removed_ids),
        "removed_ids": removed_ids,
        "attempts": attempts,
    }


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
        name = options["name"]
        tenant_id = options["tenant"]
        bucket = options["bucket"]
        result = retire_quarantined(
            tenant_id=tenant_id,
            name=name,
            bucket=bucket,
            limit=options["limit"],
            confirm=options["confirm"],
        )

        for attempt in result["attempts"]:
            job_id = attempt["job_id"]
            if attempt["status"] == "would_remove":
                self.stdout.write(
                    f"retire_quarantined: WOULD_REMOVE tenant={tenant_id} bucket={bucket} name={name} jobId={job_id}"
                )
            elif attempt["status"] == "failed":
                self.stderr.write(
                    self.style.ERROR(
                        "retire_quarantined: FAILED "
                        f"tenant={tenant_id} bucket={bucket} name={name} "
                        f"jobId={job_id} error={attempt['error']}"
                    )
                )
            else:
                self.stdout.write(
                    f"retire_quarantined: REMOVED tenant={tenant_id} bucket={bucket} name={name} jobId={job_id}"
                )

        self.stdout.write(
            "retire_quarantined: "
            f"tenant={tenant_id} name={name} matched={result['matched']} "
            f"removed={result['removed']} failed={result['failed']} remaining={result['remaining']}"
        )
