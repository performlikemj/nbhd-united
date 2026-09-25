"""Bump a tenant's OpenClaw version atomically (config + image).

Updates the tenant's openclaw_version field, regenerates and pushes
the version-appropriate config, then swaps the container image.
On failure, rolls back the version field so the command is safe to retry.

Usage:

    # Single tenant (canary)
    python manage.py bump_openclaw_version \\
        --oc-version 2026.4.15 \\
        --tenant 148ccf1c-... \\
        --image-tag openclaw-2026.4.15

    # Fleet rollout
    python manage.py bump_openclaw_version \\
        --oc-version 2026.4.15 \\
        --all --tenants UUID,UUID \\
        --image-tag openclaw-2026.4.15

    # Preview without changes
    python manage.py bump_openclaw_version \\
        --oc-version 2026.4.15 --all --tenants UUID,UUID --image-tag tag --dry-run
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.services import bump_openclaw_version_for_tenant
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Bump OpenClaw version within an explicit same-family UUID scope"

    def add_arguments(self, parser):
        parser.add_argument("--oc-version", required=True, help="Target OpenClaw version (e.g. 2026.4.15)")
        parser.add_argument("--image-tag", required=True, help="ACR image tag to deploy (e.g. openclaw-2026.4.15)")

        parser.add_argument("--tenant", help="Single tenant UUID")
        parser.add_argument("--tenants", help="Explicit comma-separated UUID list")
        parser.add_argument("--all", action="store_true", help="Skip current versions within explicit scope")

        parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")

    def handle(self, *args, **options):
        target_version = options["oc_version"]
        image_tag = options["image_tag"]
        dry_run = options["dry_run"]

        registry = getattr(settings, "AZURE_ACR_SERVER", "nbhdunited.azurecr.io")

        from apps.orchestrator.manual_scope import command_scope
        from apps.orchestrator.runtime_guard import MIGRATION_REQUIRED, manual_version_update_allowed

        try:
            ids = command_scope(options)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        selected = list(Tenant.objects.filter(pk__in=ids))
        if len(selected) != len(ids):
            raise CommandError("Unknown tenant in scope; nothing changed")
        if any(not manual_version_update_allowed(t, image_tag, target_version) for t in selected):
            raise CommandError(MIGRATION_REQUIRED)
        tenant_list = [t for t in selected if t.status == Tenant.Status.ACTIVE and t.container_id]
        if options["all"]:
            tenant_list = [t for t in tenant_list if t.openclaw_version != target_version]
        if not tenant_list:
            self.stdout.write("No eligible tenants found.")
            return

        self.stdout.write(f"{'[DRY RUN] ' if dry_run else ''}Bumping {len(tenant_list)} tenant(s) to {target_version}")

        succeeded = 0
        failed = 0
        for tenant in tenant_list:
            tid = str(tenant.id)[:8]

            if dry_run:
                self.stdout.write(
                    f"  [dry-run] {tenant.container_id} ({tid}): {tenant.openclaw_version} -> {target_version}"
                )
                continue

            old_version = tenant.openclaw_version
            try:
                self._bump_tenant(tenant, target_version, image_tag, registry)
                succeeded += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  {tenant.container_id} ({tid}): {old_version} -> {target_version}")
                )
            except Exception:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(f"  {tenant.container_id} ({tid}): FAILED reason=version_update_failed")
                )

        if not dry_run:
            self.stdout.write(f"Done: {succeeded} succeeded, {failed} failed")

    def _bump_tenant(self, tenant: Tenant, target_version: str, image_tag: str, registry: str) -> None:
        # Delegates to the shared service function so the QStash fleet-bump
        # task uses identical per-tenant semantics. See
        # apps/orchestrator/services.py:bump_openclaw_version_for_tenant
        # for atomicity guarantees (file-share snapshot/restore + DB rollback).
        bump_openclaw_version_for_tenant(tenant, target_version, image_tag, registry)
