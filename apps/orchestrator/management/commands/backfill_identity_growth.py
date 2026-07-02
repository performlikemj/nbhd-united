"""Capture a pre-migration snapshot of every tenant's SOUL.md / IDENTITY.md.

This is the ROLLBACK POINT for the sentinel-split identity migration. Before the
first ``push_identity_baseline`` (or the first ``update_tenant_config`` that
runs the new merge-push), run this once in production: for every tenant with a
container it downloads ``workspace/SOUL.md`` and ``workspace/IDENTITY.md`` and
stores them verbatim under
``Tenant.identity_growth['pre_migration_snapshot'] = {soul, identity, captured_at}``.

Idempotent: a tenant that already has the snapshot key is skipped unless
``--force`` is passed (so a re-run doesn't overwrite the true pre-migration
bytes with post-migration content).

Usage:

    python manage.py backfill_identity_growth                 # all container tenants
    python manage.py backfill_identity_growth --tenant <uuid> # single tenant
    python manage.py backfill_identity_growth --include-hibernated
    python manage.py backfill_identity_growth --dry-run
    python manage.py backfill_identity_growth --force         # re-capture even if present
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.azure_client import download_workspace_file
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

_SNAPSHOT_KEY = "pre_migration_snapshot"


class Command(BaseCommand):
    help = "Capture verbatim SOUL.md/IDENTITY.md into Tenant.identity_growth as a rollback point."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=None, help="Single tenant UUID (default: all container tenants)")
        parser.add_argument(
            "--include-hibernated",
            action="store_true",
            help="Also capture hibernated tenants (default: skip)",
        )
        parser.add_argument("--dry-run", action="store_true", help="Show what would be captured, write nothing")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-capture even if a snapshot already exists (overwrites the stored bytes)",
        )

    def handle(self, *args, **options):
        single = options["tenant"]
        include_hibernated = options["include_hibernated"]
        dry_run = options["dry_run"]
        force = options["force"]

        if single:
            qs = Tenant.objects.filter(id=single)
        else:
            qs = Tenant.objects.filter(container_id__gt="")
            if not include_hibernated:
                qs = qs.filter(hibernated_at__isnull=True)

        tenants = list(qs.select_related("user"))
        if not tenants:
            raise CommandError("No matching tenants found")

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(f"{prefix}Capturing identity snapshot for {len(tenants)} tenant(s)")

        captured = skipped = failed = 0
        for tenant in tenants:
            tid = str(tenant.id)[:8]
            growth = dict(tenant.identity_growth or {})
            if growth.get(_SNAPSHOT_KEY) and not force:
                skipped += 1
                self.stdout.write(f"  {tid}: snapshot already present — skip (use --force to overwrite)")
                continue
            try:
                soul = download_workspace_file(str(tenant.id), "workspace/SOUL.md")
                identity = download_workspace_file(str(tenant.id), "workspace/IDENTITY.md")
            except Exception as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {tid}: read FAILED — {exc}"))
                continue

            soul_len = len(soul) if soul else 0
            identity_len = len(identity) if identity else 0
            if dry_run:
                self.stdout.write(f"  {tid}: would capture soul={soul_len}c identity={identity_len}c")
                captured += 1
                continue

            growth[_SNAPSHOT_KEY] = {
                "soul": soul,
                "identity": identity,
                "captured_at": datetime.now(UTC).isoformat(),
            }
            tenant.identity_growth = growth
            tenant.save(update_fields=["identity_growth"])
            captured += 1
            self.stdout.write(self.style.SUCCESS(f"  {tid}: captured soul={soul_len}c identity={identity_len}c"))

        self.stdout.write(f"Done: {captured} captured, {skipped} skipped, {failed} failed")
        if failed:
            raise CommandError(f"{failed} tenant(s) failed — see errors above")
