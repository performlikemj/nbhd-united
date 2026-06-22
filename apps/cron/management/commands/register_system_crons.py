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
    # Hourly at :10 — true up per-tenant `estimated_cost_this_month` and
    # platform `MonthlyBudget.spent_dollars` against OpenRouter provider
    # truth. Offset from :00, :05, :15, :25 so it doesn't collide with
    # the other hourly crons. See
    # apps/billing/management/commands/reconcile_openrouter_spend.py.
    ("reconcile-openrouter-spend", "10 * * * *", "/api/cron/trigger/reconcile_openrouter_spend/"),
    # Every hour at :25 — re-push USER.md fleet-wide to keep
    # `_Current local time: ..._` fresh for cron-fired turns. Offset from
    # :00 so it doesn't collide with hibernate-idle-tenants or
    # apply-pending-configs (both at :00).
    ("refresh-user-md-fleet", "25 * * * *", "/api/cron/trigger/refresh_user_md_fleet/"),
    # Every hour — hibernate idle tenants (no messages in 2h)
    ("hibernate-idle-tenants", "0 * * * *", "/api/cron/trigger/hibernate_idle_tenants/"),
    # Daily at 07:00 UTC — clean up delivered message buffers older than 7 days
    ("cleanup-delivered-buffers", "0 7 * * *", "/api/cron/trigger/cleanup_delivered_buffers/"),
    # Hourly dispatcher for nightly extraction. Fires for each tenant whose
    # local time is 21:xx (timezone-aware). The dispatcher's own idempotency
    # guard (Tenant.last_nightly_extraction_at) prevents double-fires within
    # the same local day.
    ("nightly-extraction", "0 * * * *", "/api/cron/trigger/nightly_extraction/"),
    # Every hour — reconcile derived Fuel session crons against Postgres truth
    # for tenants on the new per-session scheduling flow (catches drift).
    ("reconcile-fuel-crons", "0 * * * *", "/api/cron/trigger/reconcile_fuel_crons/"),
    # Every hour (offset 5 min) — reconcile all managed crons against the
    # Postgres CronJob table for tenants on the postgres-cron-canonical flow.
    # Offset to avoid colliding with reconcile-fuel-crons.
    ("reconcile-tenant-crons", "5 * * * *", "/api/cron/trigger/reconcile_tenant_crons/"),
    # Every 5 min — backstop wake scheduling for kind:"at" one-off crons.
    # The hibernation path only schedules wakes when Django hibernates a
    # tenant; this sweep ensures every at-cron firing within 2h has a
    # QStash wake task queued (idempotency-keyed on fire_time so duplicates
    # collapse), so an out-of-band container restart can't cause a missed fire.
    ("ensure-at-cron-wakes", "*/5 * * * *", "/api/cron/trigger/ensure_at_cron_wakes/"),
    # Daily at 01:30 UTC — watchdog for orphaned Fuel/Gravity welcome crons.
    # Re-invokes the self-healing schedulers so a tenant whose welcome was
    # missed (gateway hiccup, agent crash mid-turn) gets retried within 24h.
    ("reconcile-welcomes", "30 1 * * *", "/api/cron/trigger/reconcile_welcomes/"),
    # Weekly Sunday 05:00 UTC — write per-tenant Gravity (finance) snapshot
    # to PillarSnapshot. Feeds the assistant's history/drill/compare tools.
    # Skips hibernated tenants; idempotent per ISO week.
    ("snapshot-gravity-weekly", "0 5 * * 0", "/api/cron/trigger/snapshot_gravity_weekly/"),
    # Monthly on 1st at 06:00 UTC — write FinanceSnapshot for every
    # finance-enabled active tenant. Idempotent per (tenant, date).
    # Powers the /api/v1/finance/snapshots/ endpoint (monthly debt/savings
    # history); without this cron that endpoint always returns an empty list.
    ("snapshot-finance-monthly", "0 6 1 * *", "/api/cron/trigger/snapshot_finance_monthly/"),
    # Hourly dispatcher for Phase 4 weekly reflection. Fires for each tenant
    # whose local time is Sunday 09:00 (timezone-aware). Synthesis runs
    # Django-side via LiteLLM — no OpenClaw container wake, no user-quota cost.
    # Idempotent per (tenant, ISO week) via Document(kind=WEEKLY) slug check.
    ("weekly-gravity-reflection", "0 * * * *", "/api/cron/trigger/weekly_gravity_reflection/"),
    # Hourly at :40 UTC — LLM-as-arbiter sweep over recent PII mints.
    # Asks Claude Haiku whether each newly-minted entity is actually a
    # person/location and promotes rejected ones to ``pii_denylist`` so
    # the redactor stops driving redaction off them. Offset from :00,
    # :05, :25 to avoid colliding with the other hourly system crons.
    # See apps/pii/arbiter.py and issue #660.
    ("pii-arbiter", "40 * * * *", "/api/cron/trigger/pii_arbiter/"),
    # Every minute — reaper for the per-tenant inbound message queue.
    # Republishes drain tasks for PendingMessage rows whose original drain
    # never ran (publish_task raised + swallowed, QStash 5xx → DLQ, worker
    # died mid-claim). Steady-state ticks are no-ops; the cron exists to
    # bound how long a stuck inbound can sit before being processed (or
    # dropped with apology, if past the staleness threshold). See
    # ``apps.router.pending_queue.reap_stuck_inbound_messages_task``.
    ("reap-stuck-inbound-messages", "* * * * *", "/api/cron/trigger/reap_stuck_inbound_messages/"),
    # Daily at 03:15 UTC — poll LINE Messaging API for monthly Push usage,
    # update the fleet-wide quota state, and dispatch the user-facing
    # fan-out (90% pre-warn, exhaustion emails + channel flips, recovery
    # emails) on any threshold crossing. The 429 tripwire on the Push
    # send paths handles intra-day exhaustion so emails go out within
    # seconds rather than waiting for the next daily poll.
    # See apps/router/line_quota.py and apps/router/line_quota_handlers.py.
    ("poll-line-quota", "15 3 * * *", "/api/cron/trigger/poll_line_quota/"),
    # Every 30 min — probe the limited-time free-model offer (OpenRouter pricing
    # + a 1-token reachability ping) and flip it on transitions. See
    # apps/billing/model_health.py.
    ("model-health-check", "*/30 * * * *", "/api/cron/trigger/model_health_check/"),
    # Every minute — global dispatcher for user-defined scheduled automations.
    # run_due_automations selects ACTIVE Automation rows whose next_run_at has
    # passed and executes them. Without this schedule the automations CRUD
    # surface is manual-run only and never fires on its configured cadence.
    # Minute granularity matches compute_next_run_at; it is a single global
    # query, so the every-minute tick is cheap (mirrors reap-stuck-inbound).
    # See apps/automations/scheduler.py:run_due_automations.
    ("run-due-automations", "* * * * *", "/api/cron/trigger/run_due_automations/"),
    # Every 5 min — sweep expired action-gate rows (flip to EXPIRED + clear the
    # stale Approve/Deny buttons on the platform message). Backstops the lazy
    # GatePollView expiry for actions the container abandons (never polls again).
    # See apps/actions/tasks.py:expire_stale_pending_actions.
    ("expire-stale-actions", "*/5 * * * *", "/api/cron/trigger/expire_stale_actions/"),
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
                registered += 1
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
