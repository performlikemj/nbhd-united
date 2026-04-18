"""Clustering helpers for lesson constellation features.

Groups approved lessons into clusters using agglomerative clustering
(average linkage) on embedding cosine similarity, then generates
cluster labels with TF-IDF-weighted tags.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from django.db import transaction

from apps.tenants.models import Tenant

from .models import Lesson

DEFAULT_CLUSTER_MIN_LESSONS = 5
# Average-linkage threshold: the mean pairwise similarity between two
# clusters must exceed this value for them to merge.  Raised to 0.78
# to prevent cross-domain lessons (e.g. DevOps + personal habits) from
# being pulled into the same cluster via a semantically bridging lesson.
CLUSTER_SIMILARITY_THRESHOLD = 0.78

# Tags describing personal behavioral patterns rather than subject domains.
# These receive 1× weight in label scoring; domain-specific tags receive
# _DOMAIN_WEIGHT_MULTIPLIER× so subject vocabulary wins over generic labels.
_BEHAVIORAL_TAGS = frozenset({
    "habits",
    "habit",
    "consistency",
    "growth",
    "mindset",
    "discipline",
    "routine",
    "productivity",
    "self-improvement",
    "personal-development",
    "resilience",
    "reflection",
    "wellbeing",
    "wellness",
    "motivation",
})

_TEXT_STOPWORDS = frozenset({
    "the", "and", "but", "for", "not", "you", "that", "this", "with",
    "have", "from", "they", "will", "your", "been", "when", "there",
    "their", "what", "which", "were", "make", "like", "just", "more",
    "also", "into", "than", "then", "some", "would", "about", "always",
    "never", "should", "could", "keep", "good", "best", "use", "using",
    "used", "can", "may", "might", "over", "each", "every", "first",
    "before", "after", "while", "since", "both", "through", "very",
    "only", "often", "most", "where", "how", "why",
})

_DOMAIN_WEIGHT_MULTIPLIER = 2.0   # multiplier for non-behavioral (domain) tags
_TEXT_TOKEN_WEIGHT = 0.4           # text tokens count as this fraction of a tag
_AMBIGUITY_MARGIN = 0.15           # swap in domain term if within 15% of behavioral top


def _extract_text_tokens(text: str, max_chars: int = 200) -> list[str]:
    """Extract meaningful word tokens from a text snippet for label scoring."""
    snippet = text[:max_chars].lower()
    tokens = re.findall(r"[a-z][a-z0-9_-]{2,}", snippet)
    return [t for t in tokens if t not in _TEXT_STOPWORDS]


def _cosine_similarity_matrix(embeddings):
    """Return (N, N) pairwise cosine-similarity matrix."""
    import numpy as np

    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized = embeddings / norms
    return normalized @ normalized.T


def _agglomerative_cluster(
    sim_matrix,
    *,
    min_similarity: float = CLUSTER_SIMILARITY_THRESHOLD,
) -> list[list[int]]:
    """Average-linkage agglomerative clustering.

    Merges the most-similar pair of clusters at each step, stopping
    when no pair exceeds *min_similarity*.  Unlike connected-component
    clustering, average linkage prevents the chaining problem where a
    single bridge edge merges unrelated groups.

    Returns a list of clusters (each a list of original row indices).
    """
    n = sim_matrix.shape[0]
    if n == 0:
        return []

    active = set(range(n))
    members: dict[int, list[int]] = {i: [i] for i in range(n)}

    # Cluster-level average similarities (initially = raw pairwise sims).
    csim: dict[int, dict[int, float]] = {
        i: {j: float(sim_matrix[i, j]) for j in range(n) if j != i}
        for i in range(n)
    }

    while len(active) > 1:
        best_sim = -1.0
        best_a, best_b = -1, -1
        for a in active:
            for b in active:
                if b <= a:
                    continue
                s = csim[a].get(b, -1.0)
                if s > best_sim:
                    best_sim = s
                    best_a, best_b = a, b

        if best_sim < min_similarity:
            break

        # Merge best_b into best_a.
        size_a = len(members[best_a])
        size_b = len(members[best_b])
        members[best_a].extend(members[best_b])
        del members[best_b]
        active.remove(best_b)

        # Recompute average-linkage similarities for the merged cluster.
        for k in active:
            if k == best_a:
                continue
            sim_ak = csim[best_a].get(k, 0.0)
            sim_bk = csim.get(best_b, {}).get(k, 0.0)
            merged = (size_a * sim_ak + size_b * sim_bk) / (size_a + size_b)
            csim[best_a][k] = merged
            csim[k][best_a] = merged

        for k in active:
            csim[k].pop(best_b, None)
        csim.pop(best_b, None)

    return [members[i] for i in active]


def cluster_lessons(tenant: Tenant) -> dict[str, int]:
    """Cluster approved lessons using agglomerative clustering (average linkage).

    Computes full pairwise cosine similarity from embeddings and merges
    clusters greedily.  Average linkage prevents the chaining problem
    where a single bridge lesson pulls unrelated topics together.

    Returns:
        {"total", "clustered", "clusters", "noise"}
    """
    import numpy as np

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

    embeddings = np.array([l.embedding for l in lessons], dtype=np.float64)
    sim_matrix = _cosine_similarity_matrix(embeddings)
    components = _agglomerative_cluster(sim_matrix, min_similarity=CLUSTER_SIMILARITY_THRESHOLD)

    updates = []
    cluster_number = 1
    clustered_count = 0
    noise_count = 0
    cluster_count = 0

    for component in components:
        if len(component) >= 2:
            for idx in component:
                lessons[idx].cluster_id = cluster_number
                updates.append(lessons[idx])
            cluster_count += 1
            cluster_number += 1
            clustered_count += len(component)
        else:
            lessons[component[0]].cluster_id = None
            updates.append(lessons[component[0]])
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
    """Generate cluster labels using TF-IDF on tags and text snippets.

    Tags frequent within a cluster but rare globally receive higher scores.
    Domain-specific tags are weighted 2× over behavioral/generic tags
    (habits, mindset, …) so subject-domain vocabulary wins over generic
    self-improvement labels.  Text tokens from lesson snippets supplement
    the tag signal when tags are sparse or overly generic.
    """
    all_lessons = list(Lesson.objects.filter(tenant=tenant, status="approved"))
    total_docs = len(all_lessons) or 1

    # Global document frequency for tags (used as IDF denominator).
    global_tag_df: Counter = Counter()
    for lesson in all_lessons:
        global_tag_df.update(set(lesson.tags))

    cluster_ids = list(
        Lesson.objects.filter(
            tenant=tenant,
            status="approved",
            cluster_id__isnull=False,
        )
        .values_list("cluster_id", flat=True)
        .distinct()
    )

    labeled = 0
    for cluster_id in cluster_ids:
        cluster_lessons = list(
            Lesson.objects.filter(
                tenant=tenant,
                status="approved",
                cluster_id=cluster_id,
            )
        )
        if not cluster_lessons:
            continue

        cluster_size = len(cluster_lessons)
        text_parts: list[str] = []
        cluster_tag_tf: Counter = Counter()

        for lesson in cluster_lessons:
            cluster_tag_tf.update(set(lesson.tags))
            text_parts.append(lesson.text or "")

        scores: dict[str, float] = {}

        # Tag TF-IDF with domain weighting.
        for tag, count in cluster_tag_tf.items():
            tf = count / cluster_size
            idf = math.log((total_docs + 1) / (global_tag_df.get(tag, 0) + 1))
            weight = 1.0 if tag.lower() in _BEHAVIORAL_TAGS else _DOMAIN_WEIGHT_MULTIPLIER
            scores[tag] = tf * idf * weight

        # Text token supplement — adds domain keywords from lesson text as
        # lower-weight candidates when not already represented by a tag.
        text_token_tf: Counter = Counter()
        for lesson in cluster_lessons:
            text_token_tf.update(set(_extract_text_tokens(lesson.text or "")))

        for token, count in text_token_tf.items():
            if token in scores:
                continue  # already covered by a tag
            tf = count / cluster_size
            idf = math.log((total_docs + 1) / (global_tag_df.get(token, 0) + 1))
            weight = 1.0 if token.lower() in _BEHAVIORAL_TAGS else _DOMAIN_WEIGHT_MULTIPLIER
            scores[token] = tf * idf * weight * _TEXT_TOKEN_WEIGHT

        if not scores:
            raw_text = " ".join(text_parts)[:500].strip()
            label = (raw_text[:40] or "Lesson cluster")[:40]
            Lesson.objects.filter(
                tenant=tenant,
                status="approved",
                cluster_id=cluster_id,
            ).update(cluster_label=label)
            labeled += 1
            continue

        sorted_terms = sorted(scores, key=lambda t: scores[t], reverse=True)

        # Ambiguity fallback: if the top term is behavioral and a domain term
        # scores within _AMBIGUITY_MARGIN of it, prefer the domain term.
        top_term = sorted_terms[0]
        if top_term.lower() in _BEHAVIORAL_TAGS:
            domain_terms = [t for t in sorted_terms if t.lower() not in _BEHAVIORAL_TAGS]
            if domain_terms:
                best_domain = domain_terms[0]
                if scores[top_term] <= scores[best_domain] * (1 + _AMBIGUITY_MARGIN):
                    sorted_terms = [best_domain] + [t for t in sorted_terms if t != best_domain]

        label = " ".join(sorted_terms[:3]).strip()

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
