"""Revoke unrevoked device tokens that have not registered recently."""

from __future__ import annotations

from datetime import UTC, datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.router.models import DeviceToken


def _parse_cutoff(value: str) -> datetime:
    cutoff = parse_datetime(value)
    if cutoff is None:
        parsed_date = parse_date(value)
        if parsed_date is None:
            raise CommandError(f"Invalid ISO-8601 cutoff: {value}")
        cutoff = datetime.combine(parsed_date, time.min)

    if timezone.is_naive(cutoff):
        cutoff = timezone.make_aware(cutoff, UTC)
    if cutoff > timezone.now():
        raise CommandError("--last-seen-before cannot be in the future")
    return cutoff


class Command(BaseCommand):
    help = "Revoke unrevoked device tokens last seen before an ISO-8601 cutoff."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--last-seen-before",
            required=True,
            help="ISO-8601 datetime or date; naive values are interpreted as UTC.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report matching tokens without revoking them.")

    def handle(self, *args, **options) -> None:
        cutoff = _parse_cutoff(options["last_seen_before"])
        queryset = DeviceToken.objects.filter(revoked_at__isnull=True, last_seen_at__lt=cutoff)
        rows = list(
            queryset.order_by("id").values(
                "id",
                "user_id",
                "tenant_id",
                "environment",
                "last_seen_at",
            )
        )

        environment_counts = {environment: 0 for environment in DeviceToken.Environment.values}
        for row in rows:
            environment_counts[row["environment"]] += 1

        self.stdout.write(f"Cutoff: {cutoff.isoformat()}")
        self.stdout.write(f"Total targeted: {len(rows)}")
        self.stdout.write("By environment:")
        for environment in DeviceToken.Environment.values:
            self.stdout.write(f"  {environment}: {environment_counts[environment]}")
        self.stdout.write(f"Distinct users affected: {len({row['user_id'] for row in rows})}")
        self.stdout.write("Tokens:")
        for row in rows:
            self.stdout.write(
                f"  id={row['id']} user_id={row['user_id']} tenant_id={row['tenant_id']} "
                f"environment={row['environment']} last_seen_at={row['last_seen_at'].isoformat()}"
            )

        if options["dry_run"]:
            self.stdout.write("DRY RUN — nothing changed")
            return

        revoked = queryset.update(revoked_at=timezone.now())
        self.stdout.write(self.style.SUCCESS(f"revoked {revoked} tokens"))
