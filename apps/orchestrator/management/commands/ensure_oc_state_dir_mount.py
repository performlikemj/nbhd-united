"""Retrofit the OpenClaw 2026.9.4 state relocation onto existing tenant
Container Apps: the ``oc-state`` EmptyDir volume + mount AND the four env
vars that point OpenClaw's state dir at it (OPENCLAW_STATE_DIR) while pinning
config + workspace back to the SMB share (OPENCLAW_CONFIG_PATH,
OPENCLAW_WORKSPACE_DIR, XDG_CACHE_HOME).

Why: 2026.9.4 moved all runtime SQLite (state/flows/tasks/plugin-state) under
resolveStateDir() and now reads it via a read-only snapshot *worker* that
FAILS on the SMB share ("SQLite read-only worker returned invalid JSON" →
the gateway refuses to boot). Moving stateDir to local ephemeral storage puts
every SQLite off SMB; the durable truth (config + workspace/memory) stays on
the share, crons re-seed from Postgres, transcripts live in Postgres.

This is a one-shot retrofit. New tenants get env + volume + mount at
provisioning; existing tenants need this because image/config bumps do NOT
rewrite container env vars.

Usage:

    # Single tenant (use this for the demo/canary before any fleet run)
    python manage.py ensure_oc_state_dir_mount --tenant 1c77c8c1-...

    # All active, non-hibernated tenants (skips hibernated to avoid waking)
    python manage.py ensure_oc_state_dir_mount --all

    # Preview without changes
    python manage.py ensure_oc_state_dir_mount --all --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.orchestrator.azure_client import ensure_oc_state_dir_mount
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Retrofit the OpenClaw 9.4 oc-state EmptyDir mount + env vars onto existing tenant containers."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--tenant", help="Single tenant UUID")
        target.add_argument("--all", action="store_true", help="All active, non-hibernated tenants")

        parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if options["tenant"]:
            tenants = Tenant.objects.filter(
                id=options["tenant"],
                container_id__gt="",
            )
        else:
            tenants = Tenant.objects.filter(
                status=Tenant.Status.ACTIVE,
                container_id__gt="",
                hibernated_at__isnull=True,
            )

        tenant_list = list(tenants)
        if not tenant_list:
            self.stdout.write("No eligible tenants found.")
            return

        self.stdout.write(
            f"{'[DRY RUN] ' if dry_run else ''}Ensuring oc-state mount + env on {len(tenant_list)} tenant(s)"
        )

        added = 0
        already_present = 0
        failed = 0
        for tenant in tenant_list:
            tid = str(tenant.id)[:8]

            if dry_run:
                self.stdout.write(f"  [dry-run] {tenant.container_id} ({tid})")
                continue

            try:
                changed = ensure_oc_state_dir_mount(tenant.container_id)
                if changed:
                    added += 1
                    self.stdout.write(self.style.SUCCESS(f"  {tenant.container_id} ({tid}): retrofitted"))
                else:
                    already_present += 1
                    self.stdout.write(f"  {tenant.container_id} ({tid}): already present")
            except Exception as e:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {tenant.container_id} ({tid}): FAILED - {e}"))

        if not dry_run:
            self.stdout.write(f"Done: {added} retrofitted, {already_present} already present, {failed} failed")
