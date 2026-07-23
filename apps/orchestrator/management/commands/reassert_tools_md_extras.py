"""Re-assert per-tenant managed rules in ``workspace/TOOLS.md``.

Usage:

    python manage.py reassert_tools_md_extras --tenant <uuid>
    python manage.py reassert_tools_md_extras --all
    python manage.py reassert_tools_md_extras --all --dry-run
"""

from __future__ import annotations

import concurrent.futures
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.services import reassert_tools_md_extras
from apps.tenants.models import Tenant

_DEFAULT_MAX_WORKERS = 5


class Command(BaseCommand):
    help = "Re-assert the managed per-tenant rules region in workspace/TOOLS.md."

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--tenant", help="Single tenant UUID")
        scope.add_argument("--all", action="store_true", help="Every active tenant")
        parser.add_argument("--dry-run", action="store_true", help="List tenants without writing anything")
        parser.add_argument(
            "--max-workers",
            type=int,
            default=_DEFAULT_MAX_WORKERS,
            help=f"Max concurrent storage uploads (default: {_DEFAULT_MAX_WORKERS})",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        max_workers = max(1, options["max_workers"])

        if options["tenant"]:
            try:
                tenant_uuid = UUID(str(options["tenant"]))
            except ValueError as exc:
                raise CommandError(f"--tenant must be a valid UUID, got: {options['tenant']!r}") from exc
            qs = Tenant.objects.filter(id=tenant_uuid)
        else:
            qs = Tenant.objects.filter(status=Tenant.Status.ACTIVE)

        tenants = list(qs.select_related("user"))
        if not tenants:
            raise CommandError("No matching tenants found")

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            f"{prefix}Re-asserting TOOLS.md extras for {len(tenants)} tenant(s) (concurrency: {max_workers})"
        )

        if dry_run:
            for tenant in tenants:
                self.stdout.write(f"  [dry-run] {tenant.container_id or '(no container)'} ({str(tenant.id)[:8]})")
            return

        refreshed = 0
        unchanged = 0
        failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(reassert_tools_md_extras, tenant): tenant for tenant in tenants}
            for future in concurrent.futures.as_completed(futures):
                tenant = futures[future]
                tid = str(tenant.id)[:8]
                label = tenant.container_id or "(no container)"
                try:
                    wrote = future.result()
                    if wrote:
                        refreshed += 1
                        self.stdout.write(self.style.SUCCESS(f"  {label} ({tid}): refreshed"))
                    else:
                        unchanged += 1
                        self.stdout.write(f"  {label} ({tid}): unchanged")
                except Exception as exc:
                    failed += 1
                    self.stderr.write(self.style.ERROR(f"  {label} ({tid}): FAILED — {exc}"))

        self.stdout.write(f"Done: {refreshed} refreshed, {unchanged} unchanged, {failed} failed")
        if failed:
            raise CommandError(f"{failed} tenant(s) failed — see errors above")
