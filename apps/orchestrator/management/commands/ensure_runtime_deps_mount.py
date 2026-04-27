"""Retrofit the plugin-runtime-deps EmptyDir mount onto existing tenant
Container Apps.

Why: OpenClaw's bundled-channel installer copies files into
``~/.openclaw/plugin-runtime-deps/`` with modes Azure File Share/SMB
doesn't support, producing EPERM on ``.buildstamp`` and a stale runtime
lock that wedges across container restarts. Mounting EmptyDir at that
path keeps the install on ephemeral local storage.

This is a one-shot retrofit. New tenants get the mount at provisioning;
hibernated tenants get it on natural wake (see ``wake_hibernated_tenant``)
or on the next image bump (see ``update_container_image``). This command
exists for active tenants that are awake and stuck.

Usage:

    # Single tenant
    python manage.py ensure_runtime_deps_mount --tenant 148ccf1c-...

    # All active, non-hibernated tenants (skips hibernated to avoid
    # waking them prematurely)
    python manage.py ensure_runtime_deps_mount --all

    # Preview without changes
    python manage.py ensure_runtime_deps_mount --all --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.orchestrator.azure_client import ensure_plugin_runtime_deps_mount
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Retrofit the plugin-runtime-deps EmptyDir mount onto existing tenant containers."

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
            f"{'[DRY RUN] ' if dry_run else ''}Ensuring plugin-runtime-deps mount on {len(tenant_list)} tenant(s)"
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
                changed = ensure_plugin_runtime_deps_mount(tenant.container_id)
                if changed:
                    added += 1
                    self.stdout.write(self.style.SUCCESS(f"  {tenant.container_id} ({tid}): mount added"))
                else:
                    already_present += 1
                    self.stdout.write(f"  {tenant.container_id} ({tid}): already present")
            except Exception as e:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {tenant.container_id} ({tid}): FAILED - {e}"))

        if not dry_run:
            self.stdout.write(f"Done: {added} mount added, {already_present} already present, {failed} failed")
