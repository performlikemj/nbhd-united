"""Roll the current OpenClaw image to every tenant in one shot.

Bridges the gap between Django's ``OPENCLAW_IMAGE_TAG`` env var (set by CI on
each deploy) and tenants' ``container_image_tag`` rows in Postgres. When
those drift — e.g. PR #408 added the ``claude`` binary, but 24 tenants were
still on 2026.4.5 because the previous fleet rollout was scoped to canary
only — features that depend on the new image silently fail at runtime.

This is the non-lazy counterpart to:

  * ``apps/router/container_updates.py`` — opportunistic per-message bump
    that only fires when a user is idle ≥2h. Slow to converge.
  * ``apps/orchestrator/hibernation.py`` — wakes a hibernated tenant onto
    the latest image (PR #384). Only fires on wake, not for active tenants.

For an immediate fleet rollout (e.g. shipping a Dockerfile change that the
BYO Anthropic flow needs at runtime), call this command. It targets every
active tenant, parallelises Azure API calls behind a thread pool capped at
five concurrent operations (Container Apps API rate limits sit around
~30 RPM per subscription), and is idempotent — tenants whose
``container_image_tag`` already matches the current ``OPENCLAW_IMAGE_TAG``
are skipped.

Usage:

    # Default — bump every active tenant whose image is stale.
    python manage.py bump_all_tenant_images

    # Include hibernated tenants. They normally pick up the new image on
    # next wake; pass this for true zero-skew rollouts.
    python manage.py bump_all_tenant_images --include-hibernated

    # Dry run — show who would be bumped without touching Azure.
    python manage.py bump_all_tenant_images --dry-run

    # Override target tag (default: settings.OPENCLAW_IMAGE_TAG).
    python manage.py bump_all_tenant_images --tag <sha-or-named-tag>
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
    help = "Roll the current OpenClaw image to every active tenant (idempotent, rate-limited)."

    def add_arguments(self, parser):
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
        eligible = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
        )
        if not include_hibernated:
            eligible = eligible.filter(hibernated_at__isnull=True)

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
