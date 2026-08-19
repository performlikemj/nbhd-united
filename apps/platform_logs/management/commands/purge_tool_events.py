"""Retention purge for tool-contract telemetry.

Telemetry answers "is this drifting right now" and "did this drift last month".
Beyond a quarter it is neither, and it is still a per-tenant row count that grows
without limit. Default retention is 90 days.

Scheduling: this is a management command on purpose — no new scheduling infra.
Trigger it the way the other sweeps are triggered (QStash → an ops endpoint, or a
manual run); see docs/agents/telemetry.md.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.platform_logs.models import ToolContractEvent

DEFAULT_RETENTION_DAYS = 90


class Command(BaseCommand):
    help = "Delete tool-contract events older than the retention window (default 90 days)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f"Retention window in days (default {DEFAULT_RETENTION_DAYS}).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5000,
            help="Rows per delete batch, so a large backlog never holds one long lock (default 5000).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted, delete nothing.")

    def handle(self, *args, **options) -> None:
        days = options["older_than_days"]
        batch_size = options["batch_size"]
        if days < 1:
            raise CommandError("--older-than-days must be at least 1")
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1")

        cutoff = timezone.now() - timedelta(days=days)
        queryset = ToolContractEvent.objects.filter(created_at__lt=cutoff)
        targeted = queryset.count()

        self.stdout.write(f"Cutoff: {cutoff.isoformat()} (older than {days}d)")
        self.stdout.write(f"Targeted: {targeted}")

        if options["dry_run"]:
            self.stdout.write("DRY RUN — nothing deleted")
            return

        deleted = 0
        while True:
            batch_ids = list(queryset.values_list("id", flat=True)[:batch_size])
            if not batch_ids:
                break
            count, _ = ToolContractEvent.objects.filter(id__in=batch_ids).delete()
            deleted += count

        self.stdout.write(self.style.SUCCESS(f"deleted {deleted} tool events"))
