"""Roll an image within an explicit UUID scope and the same runtime family.

Usage: bump_all_tenant_images --tenant UUID [--tag VERSION-SHA]
       bump_all_tenant_images --tenants UUID,UUID [--dry-run]
Use --include-hibernated only when deliberately waking those scoped tenants.
"""

from __future__ import annotations

import concurrent.futures
import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.azure_client import update_container_image
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Container Apps ARM rate limits aren't published, but empirically ~30 RPM
# per subscription is safe. With a max of ~50 active tenants today, five
# concurrent workers gives us a 10-batch rollout — fast enough to be
# operational, slow enough to not trip 429s.
_DEFAULT_MAX_WORKERS = 5


class Command(BaseCommand):
    help = "Roll the current image within an explicit tenant scope (same runtime family only)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Explicit tenant UUID")
        parser.add_argument("--tenants", help="Explicit comma-separated UUID list")
        parser.add_argument(
            "--tag",
            default=None,
            help="Image tag to deploy (default: settings.OPENCLAW_IMAGE_TAG)",
        )
        parser.add_argument(
            "--include-hibernated",
            action="store_true",
            help="Also bump hibernated tenants (default: skip — they pick up on next wake)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would happen without making Azure calls",
        )
        parser.add_argument(
            "--max-workers",
            type=int,
            default=_DEFAULT_MAX_WORKERS,
            help=f"Max concurrent Azure API calls (default: {_DEFAULT_MAX_WORKERS})",
        )
        parser.add_argument(
            "--repository",
            default="nbhd-openclaw",
            help="ACR repository name (default: nbhd-openclaw)",
        )

    def handle(self, *args, **options):
        from apps.orchestrator.manual_scope import command_scope

        try:
            ids = command_scope(options)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        selected = Tenant.objects.filter(pk__in=ids)
        if selected.count() != len(ids):
            raise CommandError("Unknown tenant in scope; nothing changed")
        target_tag = options["tag"] or getattr(settings, "OPENCLAW_IMAGE_TAG", "") or "latest"
        if not target_tag or target_tag == "latest":
            # `latest` is unsafe here — we can't compute "is this tenant
            # already on it?" without a registry digest lookup, so refuse.
            # CI sets OPENCLAW_IMAGE_TAG=<sha> after every deploy.
            raise CommandError(
                "Refusing to roll out the 'latest' tag — pass --tag <sha> "
                "or set OPENCLAW_IMAGE_TAG to a concrete tag. "
                "Otherwise this command can't compute idempotence."
            )

        registry = getattr(settings, "AZURE_ACR_SERVER", None)
        if not registry:
            raise CommandError("AZURE_ACR_SERVER is not configured")

        repository = options["repository"]
        target_image = f"{registry}/{repository}:{target_tag}"
        include_hibernated = options["include_hibernated"]
        dry_run = options["dry_run"]
        max_workers = max(1, options["max_workers"])

        # Eligible: tenants with a real container. Hibernated tenants keep
        # ``Status.ACTIVE`` — the flag is ``hibernated_at``. Default behavior
        # is to skip them since the wake hook (PR #384) pushes the current
        # image automatically; ``--include-hibernated`` forces the bump now
        # for true zero-skew rollouts. Suspended/pending/deprovisioning/
        # deleted are out of scope (containers are gone or about to be).
        eligible = selected.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
        )
        if not include_hibernated:
            eligible = eligible.filter(hibernated_at__isnull=True)

        from apps.orchestrator.runtime_guard import MIGRATION_REQUIRED, image_only_update_allowed

        # Validate the entire scope BEFORE launching any worker, including same-tag
        # partial upgrades. No tenant is mutated if one needs a migration.
        if any(not image_only_update_allowed(t, target_tag) for t in selected):
            raise CommandError(MIGRATION_REQUIRED)

        # Idempotence: skip tenants already on the target tag.
        to_bump = [t for t in eligible if (t.container_image_tag or "") != target_tag]
        skipped_idempotent = eligible.count() - len(to_bump)

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            f"{prefix}Target image: {target_image}\n"
            f"{prefix}Eligible tenants: {eligible.count()} "
            f"(active{', + hibernated' if include_hibernated else ''})\n"
            f"{prefix}Already on target tag: {skipped_idempotent}\n"
            f"{prefix}To bump: {len(to_bump)}\n"
            f"{prefix}Concurrency: {max_workers}"
        )

        if not to_bump:
            self.stdout.write(self.style.SUCCESS("Nothing to do — fleet is already on the target tag."))
            return

        if dry_run:
            for tenant in to_bump:
                self.stdout.write(
                    f"  [dry-run] {tenant.container_id} ({str(tenant.id)[:8]}): "
                    f"{tenant.container_image_tag or '(unknown)'} -> {target_tag}"
                )
            return

        succeeded = 0
        failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._azure_bump, tenant, target_image): tenant for tenant in to_bump}
            for future in concurrent.futures.as_completed(futures):
                tenant = futures[future]
                tid = str(tenant.id)[:8]
                try:
                    future.result()
                except Exception as exc:
                    failed += 1
                    self.stderr.write(self.style.ERROR(f"  {tenant.container_id} ({tid}): FAILED — {exc}"))
                    continue
                # DB write happens on the main thread so the row update
                # honors the caller's transaction context (important for
                # tests using ``TestCase`` — worker threads run on a
                # different connection that can't see the test's tx).
                Tenant.objects.filter(id=tenant.id).update(container_image_tag=target_tag)
                succeeded += 1
                self.stdout.write(self.style.SUCCESS(f"  {tenant.container_id} ({tid}): bumped to {target_tag}"))

        self.stdout.write(f"Done: {succeeded} bumped, {failed} failed, {skipped_idempotent} already current")
        if failed:
            # Non-zero exit lets CI/cron operators detect partial rollouts.
            raise CommandError(f"{failed} tenant(s) failed to bump — see errors above")

    def _azure_bump(self, tenant: Tenant, target_image: str) -> None:
        """Push the new image to a single tenant's Container App.

        Runs inside the thread pool — the DB write happens on the main
        thread once this returns successfully. Anything raised here
        surfaces in the ``future.result()`` loop as a per-tenant failure.
        """
        update_container_image(tenant.container_id, target_image)
