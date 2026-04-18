from __future__ import annotations

import numpy as np
from django.test import TestCase

from apps.tenants.services import create_tenant

from .clustering import cluster_lessons, generate_cluster_labels, refresh_constellation
from .models import Lesson
from .serializers import ConstellationNodeSerializer


class LessonClusteringServiceTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Tenant A", telegram_chat_id=11111)

    def _create_approved_lesson(self, **overrides: object) -> Lesson:
        defaults = {
            "tenant": self.tenant,
            "text": "Sample lesson text",
            "context": "",
            "source_type": "journal",
            "source_ref": "",
            "tags": ["startup", "focus"],
            "status": "approved",
            "embedding": np.random.rand(1536).tolist(),
        }
        defaults.update(overrides)
        return Lesson.objects.create(**defaults)

    @staticmethod
    def _make_embedding(base_start: int, rng, noise: float = 0.05) -> list[float]:
        """Create an embedding clustered around a 200-dim region of the 1536-dim space."""
        base = np.zeros(1536)
        base[base_start : base_start + 200] = 1.0
        return (base + rng.normal(0, noise, 1536)).tolist()

    def test_cluster_lessons_groups_similar_embeddings(self):
        """Lessons with similar embeddings should cluster together."""
        rng = np.random.default_rng(42)

        # Group A: 3 lessons around dims 0-199
        l1 = self._create_approved_lesson(
            text="Product strategy",
            tags=["strategy", "product"],
            embedding=self._make_embedding(0, rng),
        )
        l2 = self._create_approved_lesson(
            text="Another product idea",
            tags=["strategy", "roadmap"],
            embedding=self._make_embedding(0, rng),
        )
        l3 = self._create_approved_lesson(
            text="Shipping quickly",
            tags=["execution", "shipping"],
            embedding=self._make_embedding(0, rng),
        )

        # Group B: 2 lessons around dims 400-599 (orthogonal to A)
        l4 = self._create_approved_lesson(
            text="React state management",
            tags=["frontend", "react"],
            embedding=self._make_embedding(400, rng),
        )
        l5 = self._create_approved_lesson(
            text="Node backend design",
            tags=["backend", "design"],
            embedding=self._make_embedding(400, rng),
        )

        result = cluster_lessons(self.tenant)

        self.assertEqual(result, {"total": 5, "clustered": 5, "clusters": 2, "noise": 0})

        # Verify two distinct cluster IDs assigned.
        cluster_ids = set(
            Lesson.objects.filter(tenant=self.tenant, status="approved").values_list("cluster_id", flat=True)
        )
        self.assertNotIn(None, cluster_ids)
        self.assertEqual(len(cluster_ids), 2)

        # Group members share a cluster; groups are separate.
        l1.refresh_from_db()
        l2.refresh_from_db()
        l3.refresh_from_db()
        l4.refresh_from_db()
        l5.refresh_from_db()
        self.assertEqual(l1.cluster_id, l2.cluster_id)
        self.assertEqual(l1.cluster_id, l3.cluster_id)
        self.assertEqual(l4.cluster_id, l5.cluster_id)
        self.assertNotEqual(l1.cluster_id, l4.cluster_id)

    def test_cluster_lessons_prevents_chaining(self):
        """A bridge lesson must not merge two unrelated groups.

        Constructs embeddings where A-B similarity = 0.76 and B-C
        similarity = 0.76 but A-C similarity = 0.30.  Connected
        components would chain A-B-C; average linkage keeps them apart.
        """
        # Unit vectors in 3D (padded to 1536) with exact cosine similarities.
        a = np.zeros(1536)
        a[0] = 1.0

        b = np.zeros(1536)
        b[0] = 0.76
        b[1] = 0.65

        c = np.zeros(1536)
        c[0] = 0.30
        c[1] = 0.818
        c[2] = 0.491

        rng = np.random.default_rng(99)
        noise = 0.001

        l_a1 = self._create_approved_lesson(
            text="Topic A1", tags=["alpha"],
            embedding=(a + rng.normal(0, noise, 1536)).tolist(),
        )
        l_a2 = self._create_approved_lesson(
            text="Topic A2", tags=["alpha"],
            embedding=(a + rng.normal(0, noise, 1536)).tolist(),
        )
        l_bridge = self._create_approved_lesson(
            text="Bridge topic", tags=["bridge"],
            embedding=(b + rng.normal(0, noise, 1536)).tolist(),
        )
        l_c1 = self._create_approved_lesson(
            text="Topic C1", tags=["gamma"],
            embedding=(c + rng.normal(0, noise, 1536)).tolist(),
        )
        l_c2 = self._create_approved_lesson(
            text="Topic C2", tags=["gamma"],
            embedding=(c + rng.normal(0, noise, 1536)).tolist(),
        )

        cluster_lessons(self.tenant)

        l_a1.refresh_from_db()
        l_a2.refresh_from_db()
        l_c1.refresh_from_db()
        l_c2.refresh_from_db()

        # A-group and C-group must be in different clusters.
        self.assertEqual(l_a1.cluster_id, l_a2.cluster_id)
        self.assertEqual(l_c1.cluster_id, l_c2.cluster_id)
        self.assertNotEqual(l_a1.cluster_id, l_c1.cluster_id)

    def test_generate_cluster_labels_uses_distinctive_tags(self):
        self._create_approved_lesson(
            text="I learned to optimize queries",
            tags=["database", "performance", "postgres"],
            cluster_id=1,
        )
        self._create_approved_lesson(
            text="Indexes keep queries fast",
            tags=["database", "performance", "index"],
            cluster_id=1,
        )
        self._create_approved_lesson(
            text="I practiced planning in sprints",
            tags=["delivery", "planning", "weekly"],
            cluster_id=2,
        )
        self._create_approved_lesson(
            text="Sprints reduced confusion",
            tags=["delivery", "focus", "weekly"],
            cluster_id=2,
        )

        distinct_clusters = list(
            Lesson.objects.filter(tenant=self.tenant, status="approved", cluster_id__isnull=False)
            .values_list("cluster_id", flat=True)
            .distinct()
        )
        labeled = generate_cluster_labels(self.tenant)
        self.assertEqual(labeled, len(distinct_clusters))

        labels = {
            item["cluster_id"]: item["cluster_label"]
            for item in Lesson.objects.filter(tenant=self.tenant, status="approved")
            .exclude(cluster_id__isnull=True)
            .values("cluster_id", "cluster_label")
        }
        self.assertEqual(len(labels), 2)

        for label in labels.values():
            self.assertTrue(label)
            self.assertLessEqual(len(label.split()), 3)

        cluster_one_label = labels[1]
        self.assertIn("database", cluster_one_label)
        self.assertIn("performance", cluster_one_label)

    def test_cluster_lessons_skips_when_too_few_lessons(self):
        lesson_1 = self._create_approved_lesson(text="Tiny sample one", tags=["focus"])
        lesson_2 = self._create_approved_lesson(text="Tiny sample two", tags=["focus"])
        lesson_3 = self._create_approved_lesson(text="Tiny sample three", tags=["focus"])
        lesson_4 = self._create_approved_lesson(text="Tiny sample four", tags=["focus"])

        # Pretend there was existing clustering state.
        Lesson.objects.filter(id__in=[lesson_1.id, lesson_2.id, lesson_3.id, lesson_4.id]).update(
            cluster_id=42,
        )

        result = cluster_lessons(self.tenant)
        self.assertEqual(result, {"total": 4, "clustered": 0, "clusters": 0, "noise": 0})

        self.assertTrue(
            Lesson.objects.filter(tenant=self.tenant, status="approved", id=lesson_1.id, cluster_id=42).exists()
        )

    def test_refresh_constellation_computes_positions(self):
        rng = np.random.default_rng(42)
        # All 5 lessons share similar embeddings so they form a cluster.
        lessons = [
            self._create_approved_lesson(
                tags=["focus", "planning"],
                cluster_id=None,
                embedding=self._make_embedding(0, rng),
            )
            for _ in range(5)
        ]

        result = refresh_constellation(self.tenant)

        self.assertIn("total", result)
        self.assertIn("clustered", result)
        self.assertIn("clusters", result)
        self.assertIn("noise", result)
        self.assertIn("clusters_labeled", result)
        self.assertIn("positions_computed", result)
        self.assertGreaterEqual(result["clusters_labeled"], 1)
        self.assertEqual(result["positions_computed"], 5)

        serialized = ConstellationNodeSerializer(
            Lesson.objects.filter(tenant=self.tenant, status="approved"),
            many=True,
        ).data
        # Positions should be computed from PCA on embeddings
        self.assertTrue(all(item["x"] is not None for item in serialized))
        self.assertTrue(all(item["y"] is not None for item in serialized))
