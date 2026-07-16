"""One-shot fleet convergence for ``workspace/TOOLS.md``.

TOOLS.md is seed-once from the static template (``entrypoint.sh`` copies it if
missing) and — unlike AGENTS.md — has no config-apply refresh, so the whole
existing fleet still carries the legacy "Telegram is the primary channel" line
even after the template is fixed. This command surgically swaps that one line
for the channel-agnostic body on each tenant's file share via
``services.reassert_tools_md`` (which routes the write through the
``_put_share_file`` sanitize chokepoint). It's idempotent: a tenant already
converged (or whose TOOLS.md no longer contains the legacy line) is a no-op.

Share writes work whether or not the container is currently running, so
``--include-hibernated`` converges hibernated tenants too (they'd otherwise
self-heal at their next wake via the container-started hook).

Usage:

    python manage.py converge_tools_md                     # all active, awake
    python manage.py converge_tools_md --include-hibernated
    python manage.py converge_tools_md --dry-run
    python manage.py converge_tools_md --tenant <uuid>     # single
"""

from __future__ import annotations

import concurrent.futures
import logging
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.services import reassert_tools_md
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

_DEFAULT_MAX_WORKERS = 5


class Command(BaseCommand):
    help = "Converge workspace/TOOLS.md (channel-agnostic line) on every tenant's file share."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=None, help="Single tenant UUID (default: every active tenant)")
        parser.add_argument(
            "--include-hibernated",
            action="store_true",
            help="Also converge hibernated tenants (share write works while scaled to zero)",
        )
        parser.add_argument("--dry-run", action="store_true", help="List tenants without writing anything")
        parser.add_argument(
            "--max-workers",
            type=int,
            default=_DEFAULT_MAX_WORKERS,
            help=f"Max concurrent storage uploads (default: {_DEFAULT_MAX_WORKERS})",
        )

    def handle(self, *args, **options):
        include_hibernated = options["include_hibernated"]
        dry_run = options["dry_run"]
        max_workers = max(1, options["max_workers"])

        if options["tenant"]:
            try:
                tenant_uuid = UUID(str(options["tenant"]))
            except ValueError:
                raise CommandError(f"--tenant must be a valid UUID, got: {options['tenant']!r}")
            qs = Tenant.objects.filter(id=tenant_uuid)
        else:
            qs = Tenant.objects.filter(status=Tenant.Status.ACTIVE, container_id__gt="")
            if not include_hibernated:
                qs = qs.filter(hibernated_at__isnull=True)

        tenants = list(qs.select_related("user"))
        if not tenants:
            raise CommandError("No matching tenants found")

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(f"{prefix}Converging TOOLS.md for {len(tenants)} tenant(s) (concurrency: {max_workers})")

        if dry_run:
            for tenant in tenants:
                self.stdout.write(f"  [dry-run] {tenant.container_id or '(no container)'} ({str(tenant.id)[:8]})")
            return

        converged = 0
        unchanged = 0
        failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(reassert_tools_md, tenant): tenant for tenant in tenants}
            for future in concurrent.futures.as_completed(futures):
                tenant = futures[future]
                tid = str(tenant.id)[:8]
                label = tenant.container_id or "(no container)"
                try:
                    wrote = future.result()
                    if wrote:
                        converged += 1
                        self.stdout.write(self.style.SUCCESS(f"  {label} ({tid}): converged"))
                    else:
                        unchanged += 1
                        self.stdout.write(f"  {label} ({tid}): unchanged (already converged / no legacy line)")
                except Exception as exc:
                    failed += 1
                    self.stderr.write(self.style.ERROR(f"  {label} ({tid}): FAILED — {exc}"))

        self.stdout.write(f"Done: {converged} converged, {unchanged} unchanged, {failed} failed")
        if failed:
            raise CommandError(f"{failed} tenant(s) failed — see errors above")
