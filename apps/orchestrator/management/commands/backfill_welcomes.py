"""Backfill welcome crons for already-enabled tenants.

When Fuel or Gravity (finance) was enabled before the corresponding
welcome-cron feature shipped, the tenant never received the warm
intro. This command walks active tenants with the feature flag set and
calls the same ``_schedule_fuel_welcome`` / ``_schedule_finance_welcome``
helpers used by the live toggle path.

The schedulers are self-healing: a stale one-shot cron whose fire date
already passed (without successful agent self-removal) is replaced with
a fresh one. ``Tenant.welcomes_sent[feature]`` short-circuits when the
agent has confirmed delivery. So re-running this command is safe.

Usage:
    python manage.py backfill_welcomes                         # both features, all active tenants
    python manage.py backfill_welcomes --feature finance       # Gravity only
    python manage.py backfill_welcomes --feature fuel          # Fuel only
    python manage.py backfill_welcomes --tenant 148ccf1c       # single tenant by UUID prefix
    python manage.py backfill_welcomes --tenant 148ccf1c --feature finance
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand

from apps.orchestrator.welcome_scheduler import WelcomeStatus
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

        per_feature: dict[str, Counter] = {"fuel": Counter(), "finance": Counter()}

        for tenant in tenants:
            short = str(tenant.id)[:8]

            if feature in ("fuel", "both") and tenant.fuel_enabled:
                self._run_one(tenant, short, "fuel", per_feature["fuel"])

            if feature in ("finance", "both") and tenant.finance_enabled:
                self._run_one(tenant, short, "finance", per_feature["finance"])

        self.stdout.write(self.style.SUCCESS("Done."))
        for feat, counts in per_feature.items():
            if not counts:
                continue
            parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            self.stdout.write(f"  {feat}: {parts}")

    def _run_one(self, tenant, short: str, feature: str, counts: Counter) -> None:
        if feature == "fuel":
            from apps.fuel.views import _schedule_fuel_welcome as helper
        else:
            from apps.finance.views import _schedule_finance_welcome as helper

        try:
            status = helper(tenant)
        except Exception as exc:
            counts["failed"] += 1
            self.stdout.write(self.style.ERROR(f"  {feature:7s} {short} — failed: {exc}"))
            return

        # status is a WelcomeStatus enum; .value is the string key.
        key = status.value if isinstance(status, WelcomeStatus) else str(status)
        counts[key] += 1
        self.stdout.write(f"  {feature:7s} {short} — {key}")
