"""Register system-level QStash cron schedules.

Run after deploy to ensure all platform crons are scheduled in QStash.
Idempotent — existing schedules with the same cron+destination are left alone.

Usage:
    python manage.py register_system_crons [--base-url https://your-app.azurecontainerapps.io]
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

# System crons: (name, cron_expr, path)
# cron_expr is UTC
SYSTEM_CRONS = [
    # Every 60 min — push workspace files + update idle container images
    ("apply-pending-configs", "0 * * * *", "/api/cron/apply-pending-configs/"),
    # Daily at midnight UTC — reset per-day usage counters
    ("reset-daily-counters", "0 0 * * *", "/api/cron/trigger/reset_daily_counters/"),
    # Monthly on 1st at 00:05 UTC — reset monthly usage counters
    ("reset-monthly-counters", "5 0 1 * *", "/api/cron/trigger/reset_monthly_counters/"),
    # Daily at 03:00 UTC — clean up expired Telegram tokens
    ("cleanup-expired-telegram-tokens", "0 3 * * *", "/api/cron/trigger/cleanup_expired_telegram_tokens/"),
    # Daily at 04:00 UTC — refresh expiring OAuth integrations
    ("refresh-expiring-integrations", "0 4 * * *", "/api/cron/trigger/refresh_expiring_integrations/"),
    # Daily at 05:00 UTC — clean up old inbound media files
    ("cleanup-inbound-media", "0 5 * * *", "/api/cron/trigger/cleanup_inbound_media/"),
    # Daily at 02:00 UTC — expire trials that have ended
    ("expire-trials", "0 2 * * *", "/api/cron/expire-trials/"),
    # Every 30 min — repair tenants stuck without container metadata
    ("repair-stale-provisioning", "*/30 * * * *", "/api/cron/trigger/repair_stale_tenant_provisioning/"),
    # Daily at 06:30 UTC — refresh infra costs from Azure billing
    ("refresh-infra-costs", "30 6 * * *", "/api/cron/trigger/refresh_infra_costs/"),
    # Every hour — hibernate idle tenants (no messages in 2h)
    ("hibernate-idle-tenants", "0 * * * *", "/api/cron/trigger/hibernate_idle_tenants/"),
    # Daily at 07:00 UTC — clean up delivered message buffers older than 7 days
    ("cleanup-delivered-buffers", "0 7 * * *", "/api/cron/trigger/cleanup_delivered_buffers/"),
    # Daily at 21:30 UTC — extract goals/tasks/lessons from daily notes
    ("nightly-extraction", "30 21 * * *", "/api/cron/trigger/nightly_extraction/"),
    # Every hour — reconcile derived Fuel session crons against Postgres truth
    # for tenants on the new per-session scheduling flow (catches drift).
    ("reconcile-fuel-crons", "0 * * * *", "/api/cron/trigger/reconcile_fuel_crons/"),
    # Every hour (offset 5 min) — reconcile all managed crons against the
    # Postgres CronJob table for tenants on the postgres-cron-canonical flow.
    # Offset to avoid colliding with reconcile-fuel-crons.
    ("reconcile-tenant-crons", "5 * * * *", "/api/cron/trigger/reconcile_tenant_crons/"),
    # Daily at 01:30 UTC — watchdog for orphaned Fuel/Gravity welcome crons.
    # Re-invokes the self-healing schedulers so a tenant whose welcome was
    # missed (gateway hiccup, agent crash mid-turn) gets retried within 24h.
    ("reconcile-welcomes", "30 1 * * *", "/api/cron/trigger/reconcile_welcomes/"),
]


class Command(BaseCommand):
    help = "Register system QStash cron schedules (idempotent)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            type=str,
            default="",
            help="Base URL of the Django app (e.g. https://nbhd-django-westus2...azurecontainerapps.io)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be registered without making changes",
        )

    def handle(self, *args, **options):
        import httpx

        qstash_token = getattr(settings, "QSTASH_TOKEN", "")
        if not qstash_token:
            self.stderr.write("QSTASH_TOKEN not configured — cannot register crons")
            return

        base_url = options["base_url"] or getattr(settings, "DJANGO_BASE_URL", "")
        if not base_url:
            self.stderr.write("--base-url required (or set DJANGO_BASE_URL in settings)")
            return

        base_url = base_url.rstrip("/")
        dry_run = options["dry_run"]

        headers = {
            "Authorization": f"Bearer {qstash_token}",
            "Content-Type": "application/json",
        }

        # Fetch existing schedules
        resp = httpx.get("https://qstash.upstash.io/v2/schedules", headers=headers)
        resp.raise_for_status()
        existing = {s["destination"]: s for s in resp.json()}

        registered = 0
        updated = 0
        skipped = 0

        for name, cron_expr, path in SYSTEM_CRONS:
            destination = f"{base_url}{path}"

            if destination in existing:
                existing_sched = existing[destination]
                existing_cron = existing_sched.get("cron", "")
                if existing_cron == cron_expr:
                    self.stdout.write(f"  skip (unchanged): {name} → {cron_expr}")
                    skipped += 1
                    continue

                # Cron expression changed — update the existing schedule
                schedule_id = existing_sched.get("scheduleId")
                if not schedule_id:
                    self.stderr.write(f"  SKIP (no scheduleId): {name}")
                    skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(f"  [dry-run] would update: {name} — {existing_cron} → {cron_expr}")
                    updated += 1
                    continue

                # Delete old schedule and create new one with updated cron
                del_resp = httpx.delete(
                    f"https://qstash.upstash.io/v2/schedules/{schedule_id}",
                    headers=headers,
                )
                if del_resp.status_code not in (200, 204):
                    self.stderr.write(f"  FAILED to delete old schedule {name}: {del_resp.status_code} {del_resp.text}")
                    continue

                create_resp = httpx.post(
                    f"https://qstash.upstash.io/v2/schedules/{destination}",
                    headers={**headers, "Upstash-Cron": cron_expr},
                )
                if create_resp.status_code in (200, 201):
                    self.stdout.write(self.style.SUCCESS(f"  updated: {name} — {existing_cron} → {cron_expr}"))
                    updated += 1
                else:
                    self.stderr.write(f"  FAILED to recreate {name}: {create_resp.status_code} {create_resp.text}")
                continue

            if dry_run:
                self.stdout.write(f"  [dry-run] would register: {name} → {cron_expr} → {destination}")
                continue

            create_resp = httpx.post(
                f"https://qstash.upstash.io/v2/schedules/{destination}",
                headers={**headers, "Upstash-Cron": cron_expr},
            )
            if create_resp.status_code in (200, 201):
                self.stdout.write(self.style.SUCCESS(f"  registered: {name} → {cron_expr}"))
                registered += 1
            else:
                self.stderr.write(f"  FAILED: {name} — {create_resp.status_code} {create_resp.text}")

        self.stdout.write(f"\nDone: {registered} registered, {updated} updated, {skipped} unchanged")
