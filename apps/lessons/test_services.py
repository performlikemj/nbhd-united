from __future__ import annotations

from unittest import skipUnless
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase, override_settings

from apps.tenants.services import create_tenant
from .models import Lesson, LessonConnection
from .services import create_connections, find_similar_lessons, generate_embedding, search_lessons



def _vector_with_dims(first: float = 0.0, second: float = 0.0, third: float = 0.0) -> list[float]:
    vector = [0.0] * 1536
    vector[0] = first
    vector[1] = second
    vector[2] = third
    return vector


@skipUnless(connection.vendor == "postgresql", "pgvector query annotations require PostgreSQL in tests")
class LessonServicesTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Lessons Tenant", telegram_chat_id=123456)

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("apps.lessons.services.requests.post")
    def test_generate_embedding_calls_openai(self, mock_post: Mock) -> None:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {
                    "embedding": [0.123456, 0.654321],
                }
            ]
        }
        mock_post.return_value = mock_response

        result = generate_embedding("A learning insight")

        mock_post.assert_called_once()
        called_json = mock_post.call_args.kwargs["json"]
        self.assertEqual(called_json["model"], "text-embedding-3-small")
        self.assertEqual(called_json["input"], "A learning insight")
        self.assertEqual(mock_post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(result, [0.123456, 0.654321])

    def test_find_similar_lessons_with_pre_set_embeddings(self):
        source = Lesson.objects.create(
            tenant=self.tenant,
            text="Base lesson",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(1.0, 0.0, 0.0),
            tags=["t1"],
            source_ref="ref-a",
        )
        nearby = Lesson.objects.create(
            tenant=self.tenant,
            text="Nearby lesson",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(1.0, 0.1, 0.0),
            tags=["t2"],
            source_ref="ref-b",
        )
        distant = Lesson.objects.create(
            tenant=self.tenant,
            text="Distant lesson",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(0.0, 1.0, 0.0),
            tags=["t3"],
            source_ref="ref-c",
        )

        results = find_similar_lessons(source, threshold=0.75, limit=5)

        self.assertEqual([r[0].id for r in results], [nearby.id])
        self.assertGreater(results[0][1], 0.75)

    @patch("apps.lessons.services.find_similar_lessons")
    def test_create_connections_creates_bidirectional_links(self, mock_find_similar):
        source = Lesson.objects.create(
            tenant=self.tenant,
            text="Source lesson",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(1.0, 0.0, 0.0),
            tags=["t1"],
            source_ref="ref-a",
        )
        peer = Lesson.objects.create(
            tenant=self.tenant,
            text="Peer lesson",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(0.0, 1.0, 0.0),
            tags=["t2"],
            source_ref="ref-b",
        )

        mock_find_similar.return_value = [(peer, 0.91)]

        created = create_connections(source)
        self.assertEqual(created, 2)
        self.assertTrue(LessonConnection.objects.filter(from_lesson=source, to_lesson=peer).exists())
        self.assertTrue(LessonConnection.objects.filter(from_lesson=peer, to_lesson=source).exists())

        created_again = create_connections(source)
        self.assertEqual(created_again, 0)

    def test_search_lessons_returns_ranked_results(self):
        Lesson.objects.create(
            tenant=self.tenant,
            text="Top match",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(1.0, 0.0, 0.0),
            tags=["top"],
            source_ref="ref-top",
        )
        Lesson.objects.create(
            tenant=self.tenant,
            text="Second match",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(0.7, 0.7, 0.0),
            tags=["mid"],
            source_ref="ref-mid",
        )
        Lesson.objects.create(
            tenant=self.tenant,
            text="No match",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(0.0, 1.0, 0.0),
            tags=["low"],
            source_ref="ref-low",
        )

        with patch(
            "apps.lessons.services.generate_embedding",
            return_value=_vector_with_dims(1.0, 0.0, 0.0),
        ):
            results = list(search_lessons(self.tenant, "search query", limit=3))

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].text, "Top match")
        self.assertGreater(results[0].similarity, results[1].similarity)
        self.assertAlmostEqual(results[0].similarity, 1.0, places=6)
