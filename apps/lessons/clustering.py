"""Clustering helpers for lesson constellation features.

This module groups approved lessons into clusters based on explicit lesson
connections (LessonConnection similarity edges), then generates cluster labels.
"""

from __future__ import annotations

from collections import Counter

from django.db import transaction

from apps.tenants.models import Tenant

from .models import Lesson, LessonConnection

DEFAULT_CLUSTER_MIN_LESSONS = 5
CLUSTER_SIMILARITY_THRESHOLD = 0.75


def _adjacency_from_connections(
    lesson_ids: list[int],
    *,
    min_similarity: float = CLUSTER_SIMILARITY_THRESHOLD,
) -> dict[int, set[int]]:
    """Build undirected adjacency for lesson ids from high-similarity edges."""

    adjacency: dict[int, set[int]] = {lesson_id: set() for lesson_id in lesson_ids}

    if not lesson_ids:
        return adjacency

    edges = LessonConnection.objects.filter(
        from_lesson_id__in=lesson_ids,
        to_lesson_id__in=lesson_ids,
        similarity__gte=min_similarity,
    )

    for edge in edges:
        start = edge.from_lesson_id
        end = edge.to_lesson_id
        adjacency[start].add(end)
        adjacency[end].add(start)

    return adjacency


def _connected_components(lesson_ids: list[int], adjacency: dict[int, set[int]]) -> list[list[int]]:
    """Return connected components of the lesson graph."""

    visited: set[int] = set()
    components: list[list[int]] = []

    for lesson_id in lesson_ids:
        if lesson_id in visited:
            continue

        component = []
        stack = [lesson_id]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            stack.extend(adjacency.get(current, ()))

        components.append(component)

    return components


def cluster_lessons(tenant: Tenant) -> dict[str, int]:
    """Cluster approved lessons with embeddings for a tenant.

    Returns a summary:
    {
      "total": total eligible lessons,
      "clustered": lessons assigned to a cluster (size >= 2),
      "clusters": number of non-noise clusters,
      "noise": isolated lessons not in any cluster
    }
    """

    lessons = list(
        Lesson.objects.filter(
            tenant=tenant,
            status="approved",
            embedding__isnull=False,
        )
    )

    total = len(lessons)
    if total < DEFAULT_CLUSTER_MIN_LESSONS:
        return {
            "total": total,
            "clustered": 0,
            "clusters": 0,
            "noise": 0,
        }

    lesson_ids = [lesson.id for lesson in lessons]
    adjacency = _adjacency_from_connections(lesson_ids)
    components = _connected_components(lesson_ids, adjacency)

    lesson_by_id = {lesson.id: lesson for lesson in lessons}
    updates = []
    cluster_number = 1
    clustered_count = 0
    noise_count = 0
    cluster_count = 0

    for component in components:
        if len(component) >= 2:
            for lesson_id in component:
                lesson_by_id[lesson_id].cluster_id = cluster_number
                updates.append(lesson_by_id[lesson_id])
            cluster_count += 1
            cluster_number += 1
            clustered_count += len(component)
            continue

        lesson_by_id[component[0]].cluster_id = None
        updates.append(lesson_by_id[component[0]])
        noise_count += 1

    if updates:
        with transaction.atomic():
            Lesson.objects.bulk_update(updates, ["cluster_id"])

    return {
        "total": total,
        "clustered": clustered_count,
        "clusters": cluster_count,
        "noise": noise_count,
    }


def generate_cluster_labels(tenant: Tenant) -> int:
    """Generate simple label strings for each cluster from lesson tags."""

    clusters = (
        Lesson.objects.filter(
            tenant=tenant,
            status="approved",
            cluster_id__isnull=False,
        )
        .values_list("cluster_id", flat=True)
        .distinct()
    )

    labeled = 0
    for cluster_id in clusters:
        cluster_lessons = list(
            Lesson.objects.filter(
                tenant=tenant,
                status="approved",
                cluster_id=cluster_id,
            )
        )
        if not cluster_lessons:
            continue

        text_parts = []
        tags: list[str] = []
        for lesson in cluster_lessons:
            text_parts.append(lesson.text or "")
            tags.extend([tag for tag in lesson.tags if tag])

        if tags:
            common_tags = [tag for tag, _count in Counter(tags).most_common(3)]
            label = " ".join(common_tags[:3]).strip()
        else:
            raw_text = " ".join(text_parts)[:500].strip()
            label = (raw_text[:40] or "Lesson cluster")[:40]

        Lesson.objects.filter(
            tenant=tenant,
            status="approved",
            cluster_id=cluster_id,
        ).update(cluster_label=label)
        labeled += 1

    return labeled


def compute_positions(tenant: Tenant) -> int:
    """Compute 2D positions from embeddings using PCA (numpy SVD).

    Projects 1536-dim embeddings onto the top 2 principal components
    and normalizes to [-1, 1]. Positions are stored as position_x/position_y
    on each Lesson so the frontend can render semantic proximity.

    Returns the number of lessons updated.
    """
    import numpy as np

    lessons = list(
        Lesson.objects.filter(
            tenant=tenant,
            status="approved",
            embedding__isnull=False,
        )
    )

    n = len(lessons)
    if n == 0:
        return 0

    if n == 1:
        Lesson.objects.filter(pk=lessons[0].pk).update(position_x=0.0, position_y=0.0)
        return 1

    # Build embedding matrix (N x 1536)
    embeddings = np.array([lesson.embedding for lesson in lessons], dtype=np.float64)

    # Mean-center
    mean = embeddings.mean(axis=0)
    centered = embeddings - mean

    # SVD for PCA — project onto top 2 components
    _U, _S, Vt = np.linalg.svd(centered, full_matrices=False)
    projected = centered @ Vt[:2].T  # shape (N, 2)

    # Normalize each axis to [-1, 1]
    for axis in range(2):
        max_val = np.abs(projected[:, axis]).max()
        if max_val > 0:
            projected[:, axis] /= max_val

    # Bulk update
    updates = []
    for i, lesson in enumerate(lessons):
        lesson.position_x = float(projected[i, 0])
        lesson.position_y = float(projected[i, 1])
        updates.append(lesson)

    with transaction.atomic():
        Lesson.objects.bulk_update(updates, ["position_x", "position_y"])

    return n


def refresh_constellation(tenant: Tenant) -> dict[str, object]:
    """Run clustering + labeling + position computation for a tenant."""

    clustering_result = cluster_lessons(tenant)
    label_count = generate_cluster_labels(tenant)
    positions_count = compute_positions(tenant)
    return {
        **clustering_result,
        "clusters_labeled": label_count,
        "positions_computed": positions_count,
    }
