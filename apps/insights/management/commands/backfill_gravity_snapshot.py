"""One-shot backfill: write one current Gravity snapshot per eligible tenant.

Run once on deploy so the assistant's history/drill/compare tools have at
least one data point per tenant immediately, rather than waiting until the
first weekly cron fire.

Idempotent — same ISO-week uniqueness as the cron, so running multiple times
on the same day produces no duplicates.
"""

from django.core.management.base import BaseCommand

from apps.insights.tasks import snapshot_gravity_weekly_task


class Command(BaseCommand):
    help = "Backfill one Gravity snapshot per eligible tenant (idempotent)."

    def handle(self, *args, **options):
        counts = snapshot_gravity_weekly_task()
        self.stdout.write(f"written: {counts['written']}")
        self.stdout.write(f"skipped_existing: {counts['skipped_existing']}")
        self.stdout.write(f"errored: {counts['errored']}")
        self.stdout.write(self.style.SUCCESS("Backfill complete."))
