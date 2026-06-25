"""Audit and clean up Fuel (``_fuel:*``) cron duplicates on fuel tenants.

``apps/orchestrator/fuel_cron.py::regenerate_fuel_crons`` is the sole owner of
the ``_fuel:*`` namespace for BOTH flows (session and legacy). Duplicate fuel
crons accumulated on the legacy flow because ``_manage_fuel_cron``'s create was
additive and renames stranded old-named crons, while no janitor reaped the
overlap (the general hourly reconciler explicitly skips ``_fuel:``). This
command reports — and, with ``--apply``, converges — that namespace by running
the SAME reconcile the hourly task uses, so the canary (or any tenant) can be
cleaned on demand and verified.

Dry-run classifies every ``_fuel:*`` job into:
  * keep      — a desired cron, present (one copy).
  * duplicate — an extra copy of a desired name.
  * stale     — an 8-hex session cron no longer in the 48h desired window.
  * legacy    — a ``_fuel:{plan_name}`` orphan (stale/renamed plan).
  * add       — a desired cron missing from the container.

Usage:
    python manage.py cleanup_fuel_crons --tenant <uuid>            # dry-run audit
    python manage.py cleanup_fuel_crons --tenant <uuid> --apply    # converge one
    python manage.py cleanup_fuel_crons --apply                    # all session tenants
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from apps.orchestrator.fuel_cron import (
    _desired_fuel_crons,
    plan_fuel_cron_reconcile,
    regenerate_fuel_crons,
)
from apps.orchestrator.services import _extract_cron_jobs
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Audit/clean duplicate Fuel (_fuel:*) crons on fuel-enabled tenants."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            default=False,
            help="Converge the namespace (default is a dry-run audit).",
        )
        parser.add_argument(
            "--tenant",
            type=str,
            default=None,
            help="Only process a specific tenant UUID.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        tenant_filter = options.get("tenant")

        if not apply:
            self.stdout.write(self.style.WARNING("DRY RUN — pass --apply to actually converge.\n"))

        # Every fuel-enabled tenant — the reconciler owns _fuel:* for both the
        # session and legacy flows. (A non-fuel tenant has no _fuel:* crons.)
        # Filter on container_fqdn (the field the gateway resolves the box by;
        # matches reconcile_fuel_crons_task) so half-provisioned tenants with a
        # blank FQDN don't guarantee a GatewayError on every cron.list.
        tenants = (
            Tenant.objects.filter(
                status=Tenant.Status.ACTIVE,
                fuel_enabled=True,
            )
            .exclude(container_fqdn="")
            .select_related("user", "fuel_profile")
            .order_by("created_at")
        )
        if tenant_filter:
            tenants = tenants.filter(id=tenant_filter)

        tenants = list(tenants)
        self.stdout.write(f"Processing {len(tenants)} fuel-enabled tenant(s)...\n")

        totals = {"add": 0, "duplicate": 0, "stale": 0, "legacy": 0, "errors": 0, "skipped": 0}

        for tenant in tenants:
            label = f"{getattr(tenant.user, 'display_name', '') or '?'} ({str(tenant.id)[:8]})"

            # Skip hibernated/suspended — a cron.list would cold-start an idle
            # container, and the reconciler rebuilds on wake anyway.
            if getattr(tenant, "hibernated_at", None) is not None or tenant.status == Tenant.Status.SUSPENDED:
                self.stdout.write(f"  SKIP {label}: hibernated/suspended")
                totals["skipped"] += 1
                continue

            if apply:
                summary = regenerate_fuel_crons(tenant)
                totals["add"] += summary["added"]
                totals["duplicate"] += summary["duplicates_reaped"]
                totals["stale"] += summary["stale_reaped"]
                totals["legacy"] += summary["legacy_reaped"]
                totals["errors"] += summary["errors"]
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  {label}: added={summary['added']} "
                        f"reaped(dup={summary['duplicates_reaped']} "
                        f"legacy={summary['legacy_reaped']} stale={summary['stale_reaped']}) "
                        f"unchanged={summary['unchanged']} errors={summary['errors']}"
                    )
                )
            else:
                report = self._audit(tenant)
                if report is None:
                    totals["errors"] += 1
                    self.stderr.write(self.style.ERROR(f"  {label}: cron.list failed — see logs"))
                    continue
                totals["add"] += len(report["add"])
                totals["duplicate"] += report["counts"]["duplicate"]
                totals["stale"] += report["counts"]["stale"]
                totals["legacy"] += report["counts"]["legacy"]
                self.stdout.write(
                    f"  {label}: keep={report['keep']} add={len(report['add'])} "
                    f"would-reap(dup={report['counts']['duplicate']} "
                    f"legacy={report['counts']['legacy']} stale={report['counts']['stale']})"
                )
                for job, reason in report["remove"]:
                    self.stdout.write(
                        self.style.WARNING(
                            f"      {reason:<9} '{job.get('name', '?')}' "
                            f"id={str(job.get('id') or job.get('jobId') or '?')[:12]}"
                        )
                    )

            # Be gentle with gateway calls across a fleet sweep.
            time.sleep(0.5)

        verb = "Reaped" if apply else "Would reap"
        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write(
            f"{verb}: dup={totals['duplicate']} legacy={totals['legacy']} stale={totals['stale']} "
            f"| added={totals['add']} | skipped={totals['skipped']} errors={totals['errors']}"
        )
        if not apply:
            self.stdout.write(self.style.WARNING("Dry run — pass --apply to execute."))

    def _audit(self, tenant) -> dict | None:
        """Classify the tenant's ``_fuel:*`` namespace without mutating it."""
        try:
            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        except GatewayError:
            return None
        current_jobs = _extract_cron_jobs(list_result) or []
        desired_by_name = {j["name"]: j for j in _desired_fuel_crons(tenant)}
        plan = plan_fuel_cron_reconcile(desired_by_name, current_jobs)
        counts = {"duplicate": 0, "stale": 0, "legacy": 0}
        for _job, reason in plan["to_remove"]:
            counts[reason] += 1
        return {
            "keep": plan["unchanged"],
            "add": plan["to_add"],
            "remove": plan["to_remove"],
            "counts": counts,
        }
