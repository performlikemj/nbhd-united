"""Re-cluster lesson constellations for every eligible tenant.

Post-deploy backfill for the clustering threshold fix: existing tenants
were clustered under an unreachable similarity threshold (0.84) and left
with every lesson as a singleton (cluster_id = NULL).  This command re-runs
refresh_constellation() so they re-cluster under the corrected threshold.

Eligibility mirrors cluster_lessons(): a tenant qualifies once it has at
least DEFAULT_CLUSTER_MIN_LESSONS (5) approved lessons that carry an
embedding.  The command is idempotent — re-running it simply recomputes
clusters/labels/positions from current data.

Labeling (generate_cluster_labels) is pure TF-IDF over lesson tags and text
tokens — no external LLM call, so there is no per-run API cost.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from apps.lessons.clustering import DEFAULT_CLUSTER_MIN_LESSONS, refresh_constellation
from apps.lessons.models import Lesson
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Re-run refresh_constellation() for every tenant with at least "
        f"{DEFAULT_CLUSTER_MIN_LESSONS} approved embedded lessons (or a single "
        "tenant via --tenant). Idempotent. The deterministic label pass is pure "
        "TF-IDF (no API cost); the async LLM naming pass fires afterwards only in "
        "prod where QStash is configured."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report eligible tenants without writing any changes.",
        )
        parser.add_argument(
            "--tenant",
            dest="tenant",
            default=None,
            help="Backfill a single tenant by full UUID or an id prefix (canary run). "
            "Must resolve to exactly one tenant.",
        )

    def _resolve_tenant(self, ref: str) -> Tenant:
        """Resolve a tenant by exact UUID or unambiguous id prefix."""
        exact = Tenant.objects.filter(id=ref).first()
        if exact is not None:
            return exact
        matches = list(Tenant.objects.filter(id__startswith=ref)[:5])
        if not matches:
            raise CommandError(f"No tenant matches --tenant {ref!r}")
        if len(matches) > 1:
            joined = ", ".join(str(t.id)[:8] for t in matches)
            raise CommandError(f"--tenant {ref!r} is ambiguous — matches {len(matches)} tenants: {joined}")
        return matches[0]

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        tenant_ref = options["tenant"]

        if tenant_ref:
            tenants = [self._resolve_tenant(tenant_ref)]
            self.stdout.write(f"Single-tenant backfill: {str(tenants[0].id)[:8]}")
        else:
            eligible_ids = (
                Lesson.objects.filter(status="approved", embedding__isnull=False)
                .values("tenant_id")
                .annotate(n=Count("id"))
                .filter(n__gte=DEFAULT_CLUSTER_MIN_LESSONS)
                .values_list("tenant_id", flat=True)
            )
            tenants = list(Tenant.objects.filter(id__in=list(eligible_ids)))
            self.stdout.write(
                f"{len(tenants)} eligible tenant(s) (>= {DEFAULT_CLUSTER_MIN_LESSONS} approved embedded lessons)"
            )

        if dry_run:
            for tenant in tenants:
                embedded = Lesson.objects.filter(tenant=tenant, status="approved", embedding__isnull=False).count()
                self.stdout.write(f"  [dry-run] {str(tenant.id)[:8]}: {embedded} embedded lessons")
            self.stdout.write(self.style.SUCCESS("Dry run complete — no changes written."))
            return

        succeeded = 0
        failed: list[str] = []
        for tenant in tenants:
            try:
                result = refresh_constellation(tenant)
                succeeded += 1
                self.stdout.write(f"  {str(tenant.id)[:8]}: {result}")
            except Exception as e:  # noqa: BLE001 — report all, then fail non-zero
                failed.append(str(tenant.id)[:8])
                self.stdout.write(self.style.ERROR(f"  {str(tenant.id)[:8]}: FAILED — {e}"))

        self.stdout.write(self.style.SUCCESS(f"Done: {succeeded}/{len(tenants)} tenant(s) re-clustered"))

        # Non-zero exit so a caller (CI post-deploy step, operator) sees failure.
        if failed:
            raise CommandError(f"{len(failed)} tenant(s) failed: {', '.join(failed)}")
