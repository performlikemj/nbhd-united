from __future__ import annotations

"""Lesson vector services for constellation search and edge creation."""

import os
from typing import Any

import requests
from django.conf import settings
from django.db.models import FloatField, QuerySet, Value
from django.db.models.expressions import ExpressionWrapper
from pgvector.django import CosineDistance

from apps.tenants.models import Tenant

from .models import Lesson, LessonConnection

EMBEDDING_MODEL = "text-embedding-3-small"
OPENAI_EMBEDDING_URL = "https://api.openai.com/v1/embeddings"


def _resolve_openai_api_key() -> str:
    """Return OpenAI API key from settings or environment."""
    key = getattr(settings, "OPENAI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise ValueError("OPENAI API key is not configured")
    return key


def generate_embedding(text: str) -> list[float]:
    """Generate an OpenAI embedding vector for the provided text."""
    response = requests.post(
        OPENAI_EMBEDDING_URL,
        json={"input": text, "model": EMBEDDING_MODEL},
        headers={"Authorization": f"Bearer {_resolve_openai_api_key()}"},
        timeout=10,
    )
    response.raise_for_status()

    payload = response.json()
    data = payload.get("data", [])
    if not data or not data[0].get("embedding"):
        raise ValueError("OpenAI embeddings response is missing embedding data")

    embedding: list[Any] = data[0]["embedding"]
    return [float(value) for value in embedding]


def find_similar_lessons(
    lesson: Lesson,
    threshold: float = 0.75,
    limit: int = 5,
) -> list[tuple[Lesson, float]]:
    """Find similar approved lessons for the same tenant (excluding this lesson)."""
    if lesson.embedding is None:
        return []

    candidates = (
        Lesson.objects.filter(tenant=lesson.tenant, status="approved")
        .exclude(pk=lesson.pk)
        .annotate(distance=CosineDistance("embedding", lesson.embedding))
        .order_by("distance")
    )

    results: list[tuple[Lesson, float]] = []
    for candidate in candidates:
        distance = float(candidate.distance)
        similarity = 1.0 - distance
        if similarity < threshold:
            break

        results.append((candidate, similarity))
        if len(results) >= limit:
            break

    return results


def create_connections(lesson: Lesson) -> int:
    """Create bidirectional LessonConnection edges for similar approved lessons."""
    created = 0

    for similar_lesson, similarity in find_similar_lessons(lesson):
        _, created_forward = LessonConnection.objects.get_or_create(
            from_lesson=lesson,
            to_lesson=similar_lesson,
            defaults={"similarity": similarity, "connection_type": "similar"},
        )
        _, created_reverse = LessonConnection.objects.get_or_create(
            from_lesson=similar_lesson,
            to_lesson=lesson,
            defaults={"similarity": similarity, "connection_type": "similar"},
        )
        created += int(created_forward) + int(created_reverse)

    return created


def process_approved_lesson(lesson: Lesson) -> None:
    """Compute embedding for approved lesson and link it to similar lessons."""
    lesson.embedding = generate_embedding(lesson.text)
    lesson.save(update_fields=["embedding"])
    create_connections(lesson)


def search_lessons(tenant: Tenant, query: str, limit: int = 10) -> QuerySet[Lesson]:
    """Search approved lessons by semantic similarity within a tenant."""
    query_embedding = generate_embedding(query)

    similarity_expr = ExpressionWrapper(
        Value(1.0) - CosineDistance("embedding", query_embedding),
        output_field=FloatField(),
    )

    return (
        Lesson.objects.filter(tenant=tenant, status="approved", embedding__isnull=False)
        .annotate(similarity=similarity_expr)
        .order_by("-similarity")[:limit]
    )
