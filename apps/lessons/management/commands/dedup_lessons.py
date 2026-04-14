"""Remove near-duplicate lessons from the constellation.

Computes pairwise cosine similarity between all approved lessons for each
tenant, groups duplicates (similarity >= threshold), keeps the longest
lesson in each group, and deletes the rest.
"""

from __future__ import annotations

import logging

import numpy as np
from django.core.management.base import BaseCommand

from apps.lessons.models import Lesson
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

DEDUP_THRESHOLD = 0.65  # cosine similarity — anything above this is a duplicate


class Command(BaseCommand):
    help = "Remove near-duplicate lessons from the constellation"

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=str, help="Only process this tenant ID")
        parser.add_argument("--dry-run", action="store_true", help="Show duplicates without deleting")
        parser.add_argument(
            "--threshold", type=float, default=DEDUP_THRESHOLD, help="Similarity threshold (default 0.75)"
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        threshold = options.get("threshold", DEDUP_THRESHOLD)

        qs = Tenant.objects.filter(status=Tenant.Status.ACTIVE)
        if options.get("tenant"):
            qs = qs.filter(id=options["tenant"])

        for tenant in qs:
            self._dedup_tenant(tenant, threshold=threshold, dry_run=dry_run)

    def _dedup_tenant(self, tenant: Tenant, *, threshold: float, dry_run: bool) -> None:
        tid = str(tenant.id)[:8]

        # ── Remove goals from constellation (they don't belong here) ──
        from django.db.models import Q

        goal_qs = Lesson.objects.filter(tenant=tenant).filter(
            Q(tags__contains=["goal"]) | Q(context__startswith="Goal")
        )
        goal_count = goal_qs.count()
        if goal_count > 0:
            if not dry_run:
                goal_qs.delete()
            self.stdout.write(f"  [{tid}] Removed {goal_count} goal nodes from constellation")

        lessons = list(Lesson.objects.filter(tenant=tenant, status="approved", embedding__isnull=False).order_by("id"))
        if len(lessons) < 2:
            self.stdout.write(f"  [{tid}] {len(lessons)} lessons — nothing to dedup")
            return

        # Build embedding matrix
        embeddings = np.array([lesson.embedding for lesson in lessons])
        # Normalize for cosine similarity
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1  # avoid division by zero
        normalized = embeddings / norms
        similarity_matrix = normalized @ normalized.T

        # Find duplicate groups via union-find
        n = len(lessons)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(n):
            for j in range(i + 1, n):
                if similarity_matrix[i][j] >= threshold:
                    union(i, j)

        # Group by root
        groups: dict[int, list[int]] = {}
        for i in range(n):
            root = find(i)
            groups.setdefault(root, []).append(i)

        # Process duplicate groups (size > 1)
        total_removed = 0
        ids_to_delete: list[int] = []

        for root, members in groups.items():
            if len(members) <= 1:
                continue

            # Keep the longest lesson text
            member_lessons = [lessons[i] for i in members]
            keeper = max(member_lessons, key=lambda l: len(l.text))
            duplicates = [l for l in member_lessons if l.id != keeper.id]

            if dry_run:
                self.stdout.write(f"\n  [{tid}] Duplicate group (keeping: {keeper.text[:80]})")
                for dup in duplicates:
                    sim = similarity_matrix[members[0]][members[member_lessons.index(dup)]]
                    self.stdout.write(f"    REMOVE: {dup.text[:80]} (sim={sim:.3f})")

            ids_to_delete.extend(d.id for d in duplicates)
            total_removed += len(duplicates)

        if not dry_run and ids_to_delete:
            Lesson.objects.filter(id__in=ids_to_delete).delete()

            # Re-cluster
            from apps.lessons.clustering import refresh_constellation

            try:
                result = refresh_constellation(tenant)
                self.stdout.write(f"  [{tid}] Re-clustered: {result}")
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  [{tid}] Clustering failed: {e}"))

        remaining = len(lessons) - total_removed
        self.stdout.write(
            self.style.SUCCESS(f"  [{tid}] {total_removed} duplicates removed, {remaining} lessons remaining")
        )
