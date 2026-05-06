"""Suspend any active tenant that lacks entitlement.

Entitled = paid (Stripe subscription) OR on a valid (unexpired) trial.
The inverse — active but no current entitlement — is the ghost-tenant
state where a tenant should not be receiving service but is. Production
had 17 such tenants (trials ended 2026-04-15 but `is_trial` flipped
to False at some prior point without status moving to SUSPENDED, so
the daily ``expire_trials`` sweep silently skipped them).

This command is the same logic the broadened ``expire_trials`` view
runs, but invokable from a shell with ``--dry-run`` for one-shot
audit/recovery and `--apply` to actually mutate.

Usage:
    python manage.py enforce_entitlement              # dry-run (default)
    python manage.py enforce_entitlement --apply      # actually suspend
    python manage.py enforce_entitlement --tenant <uuid>  # single tenant
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.cron.views import _suspend_unentitled_tenant, _unentitled_active_tenants

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Suspend active tenants without entitlement (paid sub or unexpired trial)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually suspend matched tenants. Without this flag, runs in dry-run mode.",
        )
        parser.add_argument(
            "--tenant",
            metavar="UUID",
            help="Only process a single tenant (matched by exact id).",
        )

    def handle(self, *args, **options):
        dry_run = not options["apply"]
        tenant_filter = options.get("tenant")

        qs = _unentitled_active_tenants().select_related("user")
        if tenant_filter:
            qs = qs.filter(id=tenant_filter)

        tenants = list(qs)
        mode = "DRY RUN" if dry_run else "APPLY"
        self.stdout.write(self.style.NOTICE(f"[{mode}] Matched {len(tenants)} unentitled active tenant(s)"))

        if not tenants:
            self.stdout.write("Nothing to do.")
            return

        now = timezone.now()
        for tenant in tenants:
            email = getattr(tenant.user, "email", "?") if tenant.user_id else "?"
            trial_end = tenant.trial_ends_at.date() if tenant.trial_ends_at else "(no trial)"
            days_since = (now - tenant.trial_ends_at).days if tenant.trial_ends_at else "n/a"
            hibernated = "hibernated" if tenant.hibernated_at else "active"
            self.stdout.write(
                f"  {str(tenant.id)[:8]}  {email:40s}  trial_ended={trial_end}  "
                f"days_ago={days_since}  state={hibernated}"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDry run — re-run with --apply to suspend these tenants."))
            return

        # Apply
        suspended = 0
        crons_disabled_total = 0
        hibernated_total = 0
        for tenant in tenants:
            result = _suspend_unentitled_tenant(tenant)
            suspended += 1
            crons_disabled_total += result["crons_disabled"]
            if result["hibernated"]:
                hibernated_total += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSuspended {suspended} tenant(s) "
                f"({crons_disabled_total} cron jobs disabled, "
                f"{hibernated_total} containers hibernated)"
            )
        )
