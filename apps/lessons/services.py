from __future__ import annotations

"""Lesson vector services for constellation search and edge creation."""

import math

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import FloatField, Q, QuerySet, Value
from django.db.models.expressions import ExpressionWrapper
from pgvector.django import CosineDistance

from apps.common.openrouter import OPENROUTER_EMBEDDINGS_URL, build_openrouter_body
from apps.tenants.models import Tenant

from .models import Lesson, LessonConnection

EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMS = 1536


def _resolve_openrouter_api_key() -> str:
    """Return the platform OpenRouter key or fail closed."""
    key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError("OPENROUTER_API_KEY is not configured")
    return key


def generate_embedding(text: str, *, tenant: Tenant | None = None, seam: str = "embedding") -> list[float]:
    """Generate a 1536-dimension embedding through OpenRouter ZDR."""
    from apps.pii.egress import redact_known_values

    text = redact_known_values(tenant, text, seam=seam)
    response = requests.post(
        OPENROUTER_EMBEDDINGS_URL,
        json=build_openrouter_body(EMBEDDING_MODEL, input=text),
        headers={
            "Authorization": f"Bearer {_resolve_openrouter_api_key()}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    response.raise_for_status()

    payload = response.json()
    data = payload.get("data", [])
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("OpenRouter embeddings response is missing embedding data")

    embedding = data[0].get("embedding")
    if not isinstance(embedding, list) or len(embedding) != EMBEDDING_DIMS:
        raise ValueError(f"OpenRouter embedding must contain exactly {EMBEDDING_DIMS} dimensions")
    try:
        vector = [float(value) for value in embedding]
    except (TypeError, ValueError) as exc:
        raise ValueError("OpenRouter embedding contains a non-numeric value") from exc
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("OpenRouter embedding contains a non-finite value")
    return vector


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

        # Drop only the OUTBOUND stale auto-similarity edges this lesson
        # authored (from_lesson=lesson) — peers no longer similar under the
        # current embedding (e.g. after a rewrite). We deliberately do NOT prune
        # inbound edges (to_lesson=lesson): top-5 *membership* is asymmetric even
        # though similarity *value* is symmetric (peer Y may still rank this
        # lesson in its own top-5 while this lesson dropped Y), so deleting Y→this
        # here would destroy Y's still-valid assessment. Each lesson is
        # authoritative only over the edges it created; peers prune their own
        # outbound stale edges when they next reprocess. Only ``similar`` edges
        # are touched, so user-curated types (user_linked/builds_on/contradicts)
        # are preserved, and a still-similar peer is never dropped, so the
        # recreate count stays 0 when the similar set is unchanged.
        LessonConnection.objects.filter(
            connection_type="similar",
            from_lesson=lesson,
        ).exclude(to_lesson_id__in=current_peer_ids).delete()

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
    lesson.embedding = generate_embedding(
        lesson.text,
        tenant=lesson.tenant,
        seam="lesson_index_embedding",
    )
    lesson.save(update_fields=["embedding"])
    create_connections(lesson)


def search_lessons(tenant: Tenant, query: str, limit: int = 10) -> QuerySet[Lesson]:
    """Search approved lessons by semantic similarity within a tenant."""
    query_embedding = generate_embedding(
        query,
        tenant=tenant,
        seam="lesson_search_query_embedding",
    )

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
