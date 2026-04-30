"""Backfill Postgres CronJob rows from current container state.

One-shot migration for the postgres-cron-canonical cutover. For each
tenant: read the gateway's current cron list (or fall back to the legacy
``Tenant.cron_jobs_snapshot`` if the container is unreachable), augment
with any system crons that are missing, dedup by name (newest createdAt
wins), classify by name prefix into ``source``/``managed``, and upsert
``CronJob`` rows.

By default this command does NOT flip ``Tenant.postgres_cron_canonical``.
Pass ``--enable`` to flip the flag for the targeted tenants after a
successful backfill — typical canary/fleet rollout uses two passes
(``--dry-run``, then a real run with ``--enable``).

Usage::

    python manage.py backfill_postgres_cron_truth --tenant <uuid>
    python manage.py backfill_postgres_cron_truth --all --dry-run
    python manage.py backfill_postgres_cron_truth --tenant <uuid> --enable
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill Postgres CronJob rows from container state for the postgres-cron-canonical cutover."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Single tenant UUID to backfill")
        parser.add_argument("--all", action="store_true", help="Backfill all active tenants")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be upserted without writing.",
        )
        parser.add_argument(
            "--enable",
            action="store_true",
            help=("After a successful backfill, set tenant.postgres_cron_canonical=True. Skipped on --dry-run."),
        )

    def handle(self, *args, **opts):
        if not opts.get("tenant") and not opts.get("all"):
            raise CommandError("Pass --tenant <uuid> or --all")
        if opts.get("tenant") and opts.get("all"):
            raise CommandError("Pass either --tenant or --all, not both")

        if opts["tenant"]:
            tenants = list(Tenant.objects.filter(id=opts["tenant"]).select_related("user"))
            if not tenants:
                raise CommandError(f"Tenant {opts['tenant']} not found")
        else:
            tenants = list(Tenant.objects.filter(status="active").exclude(container_fqdn="").select_related("user"))

        self.stdout.write(f"Targeting {len(tenants)} tenant(s).\n")

        totals = {"tenants": 0, "upserted": 0, "removed": 0, "errors": 0, "enabled": 0}
        for tenant in tenants:
            try:
                result = self._backfill_one(tenant, dry_run=opts["dry_run"])
                totals["tenants"] += 1
                totals["upserted"] += result.get("upserted", 0)
                totals["removed"] += result.get("removed", 0)

                self.stdout.write(
                    f"  {str(tenant.id)[:8]} ({tenant.user.email if tenant.user else '?'}): "
                    f"upserted={result.get('upserted', 0)} "
                    f"removed={result.get('removed', 0)} "
                    f"source={result.get('source', '?')}"
                )
                if opts["enable"] and not opts["dry_run"]:
                    Tenant.objects.filter(id=tenant.id).update(postgres_cron_canonical=True)
                    totals["enabled"] += 1
                    self.stdout.write("    → flipped postgres_cron_canonical=True")
            except Exception as exc:
                logger.exception("backfill_postgres_cron_truth: tenant %s failed", tenant.id)
                totals["errors"] += 1
                self.stdout.write(self.style.ERROR(f"  {str(tenant.id)[:8]}: FAILED — {exc}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. tenants={totals['tenants']} upserted={totals['upserted']} "
                f"removed={totals['removed']} enabled={totals['enabled']} errors={totals['errors']}"
            )
        )

    def _backfill_one(self, tenant, *, dry_run: bool) -> dict:
        """Read gateway state (with snapshot fallback), augment with system seeds, upsert."""
        from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
        from apps.cron.postgres_canonical import upsert_from_gateway_jobs
        from apps.orchestrator.config_generator import build_cron_seed_jobs
        from apps.orchestrator.services import _extract_cron_jobs

        # 1. Read current container state (truth-of-record today).
        gateway_jobs: list[dict] = []
        source = "gateway"
        try:
            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
            extracted = _extract_cron_jobs(list_result) or []
            gateway_jobs = [j for j in extracted if isinstance(j, dict)]
        except GatewayError:
            # Container unavailable — fall back to legacy snapshot.
            snapshot = tenant.cron_jobs_snapshot or {}
            gateway_jobs = [j for j in snapshot.get("jobs", []) if isinstance(j, dict)]
            source = "snapshot"

        # 2. Augment with system seeds: any cron that build_cron_seed_jobs
        # produces but isn't currently present (covers the case where the
        # container was wiped and the legacy snapshot is also empty).
        gateway_names_lc = {(j.get("name") or "").lower() for j in gateway_jobs}
        seed_jobs = build_cron_seed_jobs(tenant)
        for seed in seed_jobs:
            if (seed.get("name") or "").lower() not in gateway_names_lc:
                gateway_jobs.append(seed)
                source = source + "+seeds" if source else "seeds"

        if dry_run:
            return {"upserted": 0, "removed": 0, "source": f"{source} (dry-run; {len(gateway_jobs)} jobs)"}

        result = upsert_from_gateway_jobs(tenant, gateway_jobs)
        result["source"] = source
        return result
