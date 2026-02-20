from __future__ import annotations

import numpy as np

from django.test import TestCase

from apps.tenants.services import create_tenant

from .clustering import cluster_lessons, generate_cluster_labels, refresh_constellation
from .models import Lesson, LessonConnection
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

    def test_cluster_lessons_builds_connected_components(self):
        l1 = self._create_approved_lesson(text="Learning about product strategy", tags=["strategy", "product"])
        l2 = self._create_approved_lesson(text="Another product idea", tags=["strategy", "roadmap"])
        l3 = self._create_approved_lesson(text="Shipping quickly", tags=["execution", "shipping"])
        l4 = self._create_approved_lesson(text="React state management", tags=["frontend", "react"])
        l5 = self._create_approved_lesson(text="Node backend design", tags=["backend", "design"])

        LessonConnection.objects.create(
            from_lesson=l1,
            to_lesson=l2,
            similarity=0.86,
            connection_type="similar",
        )
        LessonConnection.objects.create(
            from_lesson=l2,
            to_lesson=l3,
            similarity=0.84,
            connection_type="similar",
        )
        LessonConnection.objects.create(
            from_lesson=l4,
            to_lesson=l5,
            similarity=0.79,
            connection_type="similar",
        )

        result = cluster_lessons(self.tenant)

        self.assertEqual(result, {"total": 5, "clustered": 5, "clusters": 2, "noise": 0})

        cluster_ids = set(
            Lesson.objects.filter(tenant=self.tenant, status="approved").values_list("cluster_id", flat=True)
        )
        self.assertNotIn(None, cluster_ids)
        self.assertEqual(len(cluster_ids), 2)

    def test_generate_cluster_labels_uses_common_tags(self):
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

        labeled = generate_cluster_labels(self.tenant)
        self.assertEqual(labeled, 2)

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

    def test_refresh_constellation_runs_without_position_calculation(self):
        # Build a simple connected set so clusters can be labeled.
        l1 = self._create_approved_lesson(tags=["focus", "planning"], cluster_id=None)
        l2 = self._create_approved_lesson(tags=["focus", "planning"], cluster_id=None)
        LessonConnection.objects.create(
            from_lesson=l1,
            to_lesson=l2,
            similarity=0.9,
            connection_type="similar",
        )

        result = refresh_constellation(self.tenant)

        self.assertIn("total", result)
        self.assertIn("clustered", result)
        self.assertIn("clusters", result)
        self.assertIn("noise", result)
        self.assertIn("clusters_labeled", result)
        self.assertEqual(result["clusters_labeled"], 1)

        serialized = ConstellationNodeSerializer(
            Lesson.objects.filter(tenant=self.tenant, status="approved"),
            many=True,
        ).data
        self.assertTrue(all(item["x"] is None for item in serialized))
        self.assertTrue(all(item["y"] is None for item in serialized))
