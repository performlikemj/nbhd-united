"""Force-hibernate containers that should not be running.

Covers three cases:
1. DB says hibernated (hibernated_at != NULL) but Azure revision is active
2. Active tenant with no messages in the last 24 hours
3. Suspended tenants with active revisions
4. Orphan containers in Azure with no matching tenant record

Usage:
    python manage.py force_hibernate_stale [--dry-run]
    python manage.py force_hibernate_stale --include-orphans [--dry-run]
"""

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Force-hibernate stale containers (DB-marked hibernated, idle >24h, suspended, orphans)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List targets without hibernating",
        )
        parser.add_argument(
            "--include-orphans",
            action="store_true",
            help="Also hibernate Azure containers with no matching tenant record",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        include_orphans = options["include_orphans"]

        hibernated = 0
        failed = 0
        skipped = 0

        # --- 1. DB says hibernated but Azure might still be running ---
        db_hibernated = Tenant.objects.filter(
            hibernated_at__isnull=False,
        ).exclude(container_id="")

        self.stdout.write(f"\n--- DB-marked hibernated: {db_hibernated.count()} ---")
        for tenant in db_hibernated:
            ok = self._hibernate(tenant.container_id, tenant, dry_run)
            if ok is True:
                hibernated += 1
            elif ok is False:
                failed += 1
            else:
                skipped += 1

        # --- 2. Active tenants idle >24h (not yet caught by sweep) ---
        cutoff = timezone.now() - timezone.timedelta(hours=24)
        idle_active = (
            Tenant.objects.filter(
                status=Tenant.Status.ACTIVE,
                hibernated_at__isnull=True,
            )
            .exclude(container_id="")
            .filter(
                # last_message_at older than cutoff, or never messaged
                **{}
            )
            .extra(
                where=["(last_message_at < %s OR (last_message_at IS NULL AND provisioned_at < %s))"],
                params=[cutoff, cutoff],
            )
        )

        self.stdout.write(f"\n--- Active but idle >24h: {idle_active.count()} ---")
        for tenant in idle_active:
            ok = self._hibernate(tenant.container_id, tenant, dry_run)
            if ok is True:
                hibernated += 1
                if not dry_run:
                    Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())
            elif ok is False:
                failed += 1
            else:
                skipped += 1

        # --- 3. Suspended tenants ---
        suspended = Tenant.objects.filter(
            status=Tenant.Status.SUSPENDED,
        ).exclude(container_id="")

        self.stdout.write(f"\n--- Suspended: {suspended.count()} ---")
        for tenant in suspended:
            ok = self._hibernate(tenant.container_id, tenant, dry_run)
            if ok is True:
                hibernated += 1
            elif ok is False:
                failed += 1
            else:
                skipped += 1

        # --- 4. Orphan containers (optional) ---
        if include_orphans:
            self._handle_orphans(dry_run)

        # --- Summary ---
        if dry_run:
            self.stdout.write(f"\n[dry-run] Would hibernate {hibernated + skipped}, {failed} would fail")
        else:
            self.stdout.write(
                self.style.SUCCESS(f"\nDone: {hibernated} hibernated, {failed} failed, {skipped} already inactive")
            )

    def _hibernate(self, container_name, tenant, dry_run):
        """Hibernate a single container. Returns True/False/None (already inactive)."""
        from apps.orchestrator.azure_client import get_container_client

        try:
            client = get_container_client()
            rg = "rg-nbhd-prod"
            revisions = list(client.container_apps_revisions.list_revisions(rg, container_name))
            active_revs = [r for r in revisions if r.active]

            if not active_revs:
                self.stdout.write(f"  -- {container_name} already inactive (0 active revisions)")
                return None

            email = tenant.user.email if tenant else "orphan"
            status = tenant.status if tenant else "n/a"

            if dry_run:
                self.stdout.write(
                    f"  [dry-run] would hibernate: {container_name} "
                    f"({email}, status={status}, {len(active_revs)} active rev(s))"
                )
                return True

            for rev in active_revs:
                client.container_apps_revisions.deactivate_revision(rg, container_name, rev.name)

            self.stdout.write(
                self.style.SUCCESS(f"  hibernated: {container_name} ({email}, deactivated {len(active_revs)} rev(s))")
            )
            return True

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  FAILED: {container_name}: {e}"))
            return False

    def _handle_orphans(self, dry_run):
        """Find Azure oc-* containers with no matching tenant record."""
        from django.conf import settings

        from apps.orchestrator.azure_client import get_container_client

        self.stdout.write("\n--- Orphan containers (no DB record) ---")

        try:
            client = get_container_client()
            rg = settings.AZURE_RESOURCE_GROUP

            all_apps = client.container_apps.list_by_resource_group(rg)
            oc_apps = [app for app in all_apps if app.name.startswith("oc-")]

            known_ids = set(Tenant.objects.exclude(container_id="").values_list("container_id", flat=True))

            orphans = [app for app in oc_apps if app.name not in known_ids]
            self.stdout.write(f"Found {len(orphans)} orphan(s)")

            for app in orphans:
                ok = self._hibernate(app.name, None, dry_run)
                if ok is True:
                    self.stdout.write(f"  (orphan) {app.name}")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to scan for orphans: {e}"))
