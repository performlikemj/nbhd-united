"""Bump a tenant's OpenClaw version atomically (config + image).

Updates the tenant's openclaw_version field, regenerates and pushes
the version-appropriate config, then swaps the container image.
On failure, rolls back the version field so the command is safe to retry.

Usage:

    # Single tenant (canary)
    python manage.py bump_openclaw_version \\
        --version 2026.4.15 \\
        --tenant 148ccf1c-... \\
        --image-tag openclaw-2026.4.15

    # Fleet rollout
    python manage.py bump_openclaw_version \\
        --version 2026.4.15 \\
        --all \\
        --image-tag openclaw-2026.4.15

    # Preview without changes
    python manage.py bump_openclaw_version \\
        --version 2026.4.15 --all --image-tag tag --dry-run
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.orchestrator.azure_client import update_container_image
from apps.orchestrator.services import update_tenant_config
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Bump OpenClaw version for one or all tenants (config + image, atomic per tenant)"

    def add_arguments(self, parser):
        parser.add_argument("--version", required=True, help="Target OpenClaw version (e.g. 2026.4.15)")
        parser.add_argument("--image-tag", required=True, help="ACR image tag to deploy (e.g. openclaw-2026.4.15)")

        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--tenant", help="Single tenant UUID")
        target.add_argument("--all", action="store_true", help="All active tenants not already at target version")

        parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")

    def handle(self, *args, **options):
        target_version = options["version"]
        image_tag = options["image_tag"]
        dry_run = options["dry_run"]

        registry = getattr(settings, "AZURE_ACR_SERVER", "nbhdunited.azurecr.io")

        if options["tenant"]:
            tenants = Tenant.objects.filter(
                id=options["tenant"],
                status=Tenant.Status.ACTIVE,
                container_id__gt="",
            )
        else:
            tenants = Tenant.objects.filter(
                status=Tenant.Status.ACTIVE,
                container_id__gt="",
            ).exclude(openclaw_version=target_version)

        tenant_list = list(tenants)
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
            except Exception as e:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {tenant.container_id} ({tid}): FAILED - {e}"))

        if not dry_run:
            self.stdout.write(f"Done: {succeeded} succeeded, {failed} failed")

    def _bump_tenant(self, tenant: Tenant, target_version: str, image_tag: str, registry: str) -> None:
        old_version = tenant.openclaw_version

        # 1. Set version so config generator produces version-correct output
        tenant.openclaw_version = target_version
        tenant.save(update_fields=["openclaw_version"])

        try:
            # 2. Regenerate and push config + workspace files
            update_tenant_config(str(tenant.id))

            # 3. Update container image (triggers new revision)
            image = f"{registry}/nbhd-openclaw:{image_tag}"
            update_container_image(tenant.container_id, image)

            # 4. Record image tag
            tenant.container_image_tag = image_tag
            tenant.save(update_fields=["container_image_tag"])
        except Exception:
            # Rollback version so tenant stays consistent
            tenant.openclaw_version = old_version
            tenant.save(update_fields=["openclaw_version"])
            raise
