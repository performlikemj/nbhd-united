"""Backfill welcome crons for already-enabled tenants.

When Fuel or Gravity (finance) was enabled before the corresponding
welcome-cron feature shipped, the tenant never received the warm
intro. This command walks active tenants with the feature flag set and
calls the same ``_schedule_fuel_welcome`` / ``_schedule_finance_welcome``
helpers used by the live toggle path.

The schedulers are idempotent (they check ``cron.list`` for an existing
``_fuel:welcome`` / ``_finance:welcome`` job and skip when one is
pending), so re-running this command is safe — it won't double-schedule
tenants that already have a welcome queued or have already received one.

Usage:
    python manage.py backfill_welcomes                         # both features, all active tenants
    python manage.py backfill_welcomes --feature finance       # Gravity only
    python manage.py backfill_welcomes --feature fuel          # Fuel only
    python manage.py backfill_welcomes --tenant 148ccf1c       # single tenant by UUID prefix
    python manage.py backfill_welcomes --tenant 148ccf1c --feature finance
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Schedule welcome crons for active tenants who never received them"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            type=str,
            default="",
            help="Limit to a single tenant by UUID (or UUID prefix).",
        )
        parser.add_argument(
            "--feature",
            choices=["fuel", "finance", "both"],
            default="both",
            help="Which feature's welcome to backfill (default: both).",
        )

    def handle(self, *args, **options):
        tenant_arg = (options.get("tenant") or "").strip()
        feature = options["feature"]

        qs = Tenant.objects.select_related("user").filter(status=Tenant.Status.ACTIVE).exclude(container_id="")
        if tenant_arg:
            qs = qs.filter(id__startswith=tenant_arg)

        tenants = list(qs)
        self.stdout.write(f"Backfilling welcomes for {len(tenants)} tenant(s) (feature={feature})...")

        scheduled_fuel = 0
        scheduled_finance = 0
        failed = 0

        for tenant in tenants:
            short = str(tenant.id)[:8]

            if feature in ("fuel", "both") and tenant.fuel_enabled:
                try:
                    from apps.fuel.views import _schedule_fuel_welcome

                    _schedule_fuel_welcome(tenant)
                    scheduled_fuel += 1
                    self.stdout.write(f"  fuel:    {short}")
                except Exception as exc:
                    failed += 1
                    self.stdout.write(self.style.ERROR(f"  fuel:    {short} — {exc}"))

            if feature in ("finance", "both") and tenant.finance_enabled:
                try:
                    from apps.finance.views import _schedule_finance_welcome

                    _schedule_finance_welcome(tenant)
                    scheduled_finance += 1
                    self.stdout.write(f"  finance: {short}")
                except Exception as exc:
                    failed += 1
                    self.stdout.write(self.style.ERROR(f"  finance: {short} — {exc}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: fuel={scheduled_fuel}, finance={scheduled_finance}, errors={failed}"
            )
        )
        self.stdout.write(
            "Note: idempotency means a tenant whose welcome cron is already "
            "pending or has already fired and self-removed won't get another "
            "scheduled here — output above counts schedule attempts, not "
            "necessarily fresh welcomes."
        )
