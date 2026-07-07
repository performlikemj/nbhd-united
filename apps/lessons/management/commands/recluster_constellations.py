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

from django.core.management.base import BaseCommand
from django.db.models import Count

from apps.lessons.clustering import DEFAULT_CLUSTER_MIN_LESSONS, refresh_constellation
from apps.lessons.models import Lesson
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Re-run refresh_constellation() for every tenant with at least "
        f"{DEFAULT_CLUSTER_MIN_LESSONS} approved embedded lessons. Idempotent. "
        "Labeling is pure TF-IDF (tags + text tokens) — no LLM, no API cost."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report eligible tenants without writing any changes.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

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
        for tenant in tenants:
            try:
                result = refresh_constellation(tenant)
                succeeded += 1
                self.stdout.write(f"  {str(tenant.id)[:8]}: {result}")
            except Exception as e:  # noqa: BLE001 — report and continue per tenant
                self.stdout.write(self.style.ERROR(f"  {str(tenant.id)[:8]}: FAILED — {e}"))

        self.stdout.write(self.style.SUCCESS(f"Done: {succeeded}/{len(tenants)} tenant(s) re-clustered"))
