from __future__ import annotations

"""Lesson vector services for constellation search and edge creation."""

import os
from typing import Any

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import FloatField, Q, QuerySet, Value
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
    """Reconcile bidirectional similarity edges for the lesson against its current embedding.

    The lesson's auto-generated ``similar`` edges are brought in line with the
    current embedding: edges to peers that are no longer similar (e.g. after a
    rewrite) are removed so they can't draw spurious links or mask the correct
    affinity edges in the constellation/galaxy views, and surviving similar
    edges have their weight refreshed. User-curated edge types
    (``user_linked``/``builds_on``/``contradicts``) are preserved.
    """
    created = 0

    with transaction.atomic():
        similar = find_similar_lessons(lesson)
        current_peer_ids = {peer.pk for peer, _ in similar}

        # Drop the lesson's stale auto-similarity edges — peers that are no
        # longer similar under the current embedding (e.g. after a rewrite) —
        # so they can't draw spurious links or mask the correct affinity edge.
        # Only ``similar`` edges are touched, so user-curated edge types
        # (user_linked/builds_on/contradicts) are preserved. We never delete an
        # edge to a peer that is still similar, so the recreate count stays 0
        # when the similar set is unchanged.
        LessonConnection.objects.filter(connection_type="similar").filter(
            Q(from_lesson=lesson) & ~Q(to_lesson_id__in=current_peer_ids)
            | Q(to_lesson=lesson) & ~Q(from_lesson_id__in=current_peer_ids)
        ).delete()

        for similar_lesson, similarity in similar:
            # Key on the unique (from, to) pair; if a user-curated edge already
            # exists for the pair, leave it untouched rather than overwrite it.
            # Surviving ``similar`` edges have their similarity refreshed so the
            # weight tracks the current embedding.
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
            LessonConnection.objects.filter(
                Q(from_lesson=lesson, to_lesson=similar_lesson) | Q(from_lesson=similar_lesson, to_lesson=lesson),
                connection_type="similar",
            ).update(similarity=similarity)
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
        .prefetch_related("journal_entries", "tutoring_sessions")
        .order_by("-similarity")[:limit]
    )
