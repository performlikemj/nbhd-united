"""Clustering helpers for lesson constellation features.

Groups approved lessons into clusters using agglomerative clustering
(average linkage) on embedding cosine similarity, then generates
cluster labels with TF-IDF-weighted tags.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

from django.conf import settings
from django.db import transaction

from apps.tenants.models import Tenant

from .models import Lesson

logger = logging.getLogger(__name__)

DEFAULT_CLUSTER_MIN_LESSONS = 5
# Average-linkage threshold: the mean pairwise similarity between two
# clusters must exceed this value for them to merge.
#
# Calibrated 2026-07-07 by sweeping the REAL prod pgvector similarity
# matrices of two tenants (an 111-lesson tenant and an 11-lesson tenant)
# — reproducing the live _agglomerative_cluster/_eject_outliers output
# exactly, then human-judging cluster coherence at each setting.  On
# OpenAI text-embedding-3-small (1536-dim, the model that generates lesson
# embeddings) inter-lesson cosine tops out ~0.76 and averages ~0.52; the
# average nearest-neighbour similarity is ~0.516.  At 0.62 only outright
# near-duplicates merged: the 111-lesson tenant produced 7 pairs at 12.6%
# coverage, no real same-theme groups.  At 0.50 the same-theme groups
# finally merge — ~20 human-judged-coherent clusters at 51% coverage on
# the 111-lesson tenant and 73% on the 11-lesson tenant, with no
# cross-domain mush.  0.50 sits deliberately just above the ~0.516 mean
# nearest-neighbour, so the coherence eject pass below AND the regression
# tests in test_clustering.py (floor pin at 0.40, mid-scale merge at 0.55)
# are the guardrails against drifting into the noise floor.  A prior value
# of 0.84 was mathematically unreachable against these embeddings and
# clustered nothing fleet-wide; it only "passed" in tests because synthetic
# fixtures packed vectors into a low-dim region where cosine trivially
# exceeds 0.84.
CLUSTER_SIMILARITY_THRESHOLD = 0.50

# Maximum lessons per cluster.  Prevents mega-clusters that absorb
# loosely related topics.  When a merge would exceed this, skip it.
# Raised 8→12 alongside the 0.50 recalibration: at 0.50 genuine same-theme
# groups are larger than the near-duplicate pairs 0.62 produced, and 12 was
# the size at which the swept clusters stayed coherent without chaining.
MAX_CLUSTER_SIZE = 12

# Minimum pairwise similarity for a lesson to stay in a cluster during
# the post-clustering coherence check.  Lessons whose median similarity to
# cluster-mates falls below this are ejected as noise.  Held at
# threshold − 0.10 (0.40) so the coherence pass trims genuine outliers that
# slipped in through average-linkage averaging without dissolving clusters
# that legitimately formed at real-embedding similarities (~0.52 average).
COHERENCE_MIN_SIMILARITY = 0.40

# Tags describing personal behavioral patterns rather than subject domains.
# These receive 1× weight in label scoring; domain-specific tags receive
# _DOMAIN_WEIGHT_MULTIPLIER× so subject vocabulary wins over generic labels.
_BEHAVIORAL_TAGS = frozenset(
    {
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
    }
)

# Common English function/filler words that must never become a cluster
# name.  A cluster's raw lesson text is full of these; a 65-word list (the
# prior size) leaked function words like "across", "daily", "in-person" and
# "replies" into labels, reading as word salad.  This ~280-word list covers
# articles, pronouns, prepositions, conjunctions, auxiliary/modal verbs,
# common adverbs, time/quantity fillers, and the specific leak words seen in
# the bad-label screenshot.  Applied only to TEXT tokens (tagless clusters);
# curated tags are trusted and scored on their own.
_TEXT_STOPWORDS = frozenset(
    {
        # articles / determiners / quantifiers
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "some",
        "any",
        "all",
        "both",
        "each",
        "every",
        "either",
        "neither",
        "another",
        "other",
        "such",
        "no",
        "none",
        "many",
        "much",
        "most",
        "more",
        "less",
        "few",
        "several",
        "enough",
        "own",
        "same",
        "whole",
        "half",
        "lot",
        "lots",
        "bit",
        "kind",
        "sort",
        "type",
        "part",
        "piece",
        "couple",
        "plenty",
        # pronouns
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "we",
        "us",
        "our",
        "ours",
        "ourselves",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        "he",
        "him",
        "his",
        "himself",
        "she",
        "her",
        "hers",
        "herself",
        "it",
        "its",
        "itself",
        "they",
        "them",
        "their",
        "theirs",
        "themselves",
        "who",
        "whom",
        "whose",
        "which",
        "what",
        "whatever",
        "whoever",
        "someone",
        "somebody",
        "something",
        "anyone",
        "anybody",
        "anything",
        "everyone",
        "everybody",
        "everything",
        "nobody",
        "nothing",
        "one",
        "ones",
        # prepositions / particles
        "of",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "about",
        "against",
        "between",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "to",
        "from",
        "up",
        "down",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "onto",
        "upon",
        "within",
        "without",
        "toward",
        "towards",
        "across",
        "along",
        "around",
        "behind",
        "beside",
        "beyond",
        "near",
        "past",
        "per",
        "via",
        "amid",
        "among",
        "unto",
        "throughout",
        # conjunctions
        "and",
        "but",
        "or",
        "nor",
        "so",
        "yet",
        "because",
        "as",
        "until",
        "while",
        "although",
        "though",
        "whereas",
        "since",
        "unless",
        "whether",
        "if",
        "then",
        "than",
        "else",
        "hence",
        "thus",
        "therefore",
        "however",
        "moreover",
        "meanwhile",
        "besides",
        "otherwise",
        # auxiliary / modal / common verbs
        "be",
        "am",
        "is",
        "are",
        "was",
        "were",
        "been",
        "being",
        "have",
        "has",
        "had",
        "having",
        "do",
        "does",
        "did",
        "doing",
        "done",
        "will",
        "would",
        "shall",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        "ought",
        "get",
        "gets",
        "got",
        "getting",
        "make",
        "makes",
        "made",
        "making",
        "go",
        "goes",
        "going",
        "went",
        "come",
        "comes",
        "came",
        "coming",
        "keep",
        "keeps",
        "kept",
        "let",
        "lets",
        "put",
        "take",
        "takes",
        "took",
        "use",
        "uses",
        "used",
        "using",
        "want",
        "wants",
        "need",
        "needs",
        "try",
        "tried",
        "trying",
        "seem",
        "seems",
        "feel",
        "feels",
        "felt",
        "say",
        "says",
        "said",
        "tell",
        "told",
        "give",
        "gave",
        "given",
        "reply",
        "replies",
        "replied",
        "replying",
        # adverbs / intensifiers / fillers
        "not",
        "only",
        "just",
        "also",
        "too",
        "very",
        "really",
        "quite",
        "rather",
        "almost",
        "always",
        "never",
        "often",
        "sometimes",
        "usually",
        "already",
        "still",
        "even",
        "ever",
        "once",
        "twice",
        "back",
        "here",
        "there",
        "where",
        "when",
        "how",
        "why",
        "now",
        "soon",
        "later",
        "early",
        "late",
        "well",
        "far",
        "away",
        "maybe",
        "perhaps",
        "actually",
        "basically",
        "literally",
        "simply",
        "merely",
        "mostly",
        "generally",
        "especially",
        "particularly",
        "instead",
        "anyway",
        "somehow",
        "somewhat",
        "overall",
        # time / quantity fillers (the leak words from the bad-label screenshot)
        "day",
        "days",
        "daily",
        "today",
        "tomorrow",
        "yesterday",
        "week",
        "weekly",
        "weeks",
        "month",
        "monthly",
        "year",
        "yearly",
        "time",
        "times",
        "morning",
        "evening",
        "night",
        "afternoon",
        "hour",
        "hours",
        "minute",
        "minutes",
        "in-person",
        "late-night",
        "everyday",
        "weekday",
        "weekend",
        # generic evaluatives
        "good",
        "bad",
        "best",
        "better",
        "worse",
        "worst",
        "great",
        "nice",
        "okay",
        "fine",
        "sure",
        "new",
        "old",
        "big",
        "small",
        "long",
        "short",
        "high",
        "low",
        "next",
        "last",
        "first",
        "second",
        "third",
        "little",
        "able",
        "thing",
        "things",
        "stuff",
        "way",
        "ways",
    }
)

_DOMAIN_WEIGHT_MULTIPLIER = 2.0  # multiplier for non-behavioral (domain) tags


def _extract_text_tokens(text: str, max_chars: int = 240) -> list[str]:
    """Ordered, stopword-filtered word tokens from a text snippet.

    Order is preserved (with repeats) so adjacent-token bigrams can be built
    from the result — a good bigram ("weight tracking") reads far better as a
    label than the top-3 unigram salad the old labeler produced.
    """
    snippet = text[:max_chars].lower()
    tokens = re.findall(r"[a-z][a-z0-9_-]{2,}", snippet)
    return [t for t in tokens if t not in _TEXT_STOPWORDS]


def _bigrams(tokens: list[str]) -> list[str]:
    """Adjacent-token bigrams from an ordered token list ("a b c" → "a b", "b c")."""
    return [f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)]


def _titlecase_label(term: str) -> str:
    """Render a term/bigram as a display name: Title Case, no record separators.

    Never emits '·' — the iOS HUD renders labels as "Inside · <label>", so a
    '·' inside the label would read as a double separator.
    """
    cleaned = term.replace("·", " ").strip()
    return " ".join(part.title() for part in cleaned.split())


def _format_cluster_label(terms: list[str], *, max_len: int = 40) -> str:
    """Join up to two ranked terms as a Title-Case name ("Fitness & Health").

    Drops to a single term if two would exceed ``max_len``; hard-truncates a
    lone over-long term so the DB's 40-char expectation always holds.
    """
    names = [_titlecase_label(t) for t in terms[:2] if t.strip()]
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 2:
        joined = f"{names[0]} & {names[1]}"
        if len(joined) <= max_len:
            return joined
    return names[0][:max_len]


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
    max_size: int = MAX_CLUSTER_SIZE,
) -> list[list[int]]:
    """Average-linkage agglomerative clustering with size cap.

    Merges the most-similar pair of clusters at each step, stopping
    when no pair exceeds *min_similarity*.  Unlike connected-component
    clustering, average linkage prevents the chaining problem where a
    single bridge edge merges unrelated groups.

    Merges that would create a cluster larger than *max_size* are
    skipped (the pair is blacklisted for the rest of the run).

    Returns a list of clusters (each a list of original row indices).
    """
    n = sim_matrix.shape[0]
    if n == 0:
        return []

    active = set(range(n))
    members: dict[int, list[int]] = {i: [i] for i in range(n)}
    blocked: set[tuple[int, int]] = set()

    # Cluster-level average similarities (initially = raw pairwise sims).
    csim: dict[int, dict[int, float]] = {i: {j: float(sim_matrix[i, j]) for j in range(n) if j != i} for i in range(n)}

    while len(active) > 1:
        best_sim = -1.0
        best_a, best_b = -1, -1
        for a in active:
            for b in active:
                if b <= a:
                    continue
                pair = (min(a, b), max(a, b))
                if pair in blocked:
                    continue
                s = csim[a].get(b, -1.0)
                if s > best_sim:
                    best_sim = s
                    best_a, best_b = a, b

        if best_sim < min_similarity:
            break

        # Size cap: skip merge if it would exceed max_size.
        if len(members[best_a]) + len(members[best_b]) > max_size:
            blocked.add((min(best_a, best_b), max(best_a, best_b)))
            continue

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


def _eject_outliers(
    clusters: list[list[int]],
    sim_matrix,
    *,
    min_coherence: float = COHERENCE_MIN_SIMILARITY,
) -> list[list[int]]:
    """Remove lessons whose median similarity to cluster-mates is too low.

    Ejected lessons become singleton clusters (noise).  This catches
    the case where a lesson slipped in through average-linkage averaging
    despite being semantically distant from most of the cluster.
    """
    result: list[list[int]] = []
    for cluster in clusters:
        if len(cluster) <= 2:
            result.append(cluster)
            continue

        kept: list[int] = []
        ejected: list[int] = []
        for idx in cluster:
            # Compute median similarity to other members
            sims = [float(sim_matrix[idx, other]) for other in cluster if other != idx]
            sims.sort()
            median_sim = sims[len(sims) // 2] if sims else 0.0
            if median_sim >= min_coherence:
                kept.append(idx)
            else:
                ejected.append(idx)

        if kept:
            result.append(kept)
        for e in ejected:
            result.append([e])

    return result


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
    components = _agglomerative_cluster(
        sim_matrix,
        min_similarity=CLUSTER_SIMILARITY_THRESHOLD,
        max_size=MAX_CLUSTER_SIZE,
    )
    # Eject outlier lessons that slipped in through averaging
    components = _eject_outliers(components, sim_matrix, min_coherence=COHERENCE_MIN_SIMILARITY)

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


def _cluster_candidate_terms(
    cluster_lessons: list[Lesson],
    total_docs: int,
    global_tag_df: Counter,
) -> tuple[list[str], bool]:
    """Rank candidate label terms for one cluster (the hardened deterministic floor).

    Returns ``(terms, used_tags)`` — an ordered list of the best candidate
    strings (tags, or text bigrams/unigrams) and whether they came from
    curated tags (``True``) or raw lesson text (``False``). Rules:

    * **Tags-first.** If ANY lesson in the cluster carries a tag, candidates
      are drawn from tags only; raw text is used solely for tagless clusters.
      Curated tags read far cleaner than text tokens.
    * **Majority support.** A candidate must appear in ``ceil(size/2)`` of the
      cluster's lessons — this kills one-off proper-noun leaks (a single
      lesson's ``angellist``/``wellfound``). If nothing clears majority the
      highest-support candidates are kept so the cluster still gets a name.
    * **Text prefers bigrams.** Adjacent-token bigrams ("cold outreach") are
      scored before unigrams; unigrams are used only when no bigram clears
      majority, so labels stop reading as unigram salad.
    * **Tags rank by (support, TF-IDF·domain-weight).** Shared, domain-specific
      vocabulary wins over a globally-rare one-off — the failure mode where IDF
      dominates in tiny clusters and hands the label to junk.
    """
    size = len(cluster_lessons)
    majority = math.ceil(size / 2)
    has_tags = any(lesson.tags for lesson in cluster_lessons)

    if has_tags:
        tag_df: Counter = Counter()
        for lesson in cluster_lessons:
            tag_df.update(set(lesson.tags))

        def _tag_score(tag: str, df: int) -> float:
            tf = df / size
            idf = math.log((total_docs + 1) / (global_tag_df.get(tag, 0) + 1))
            weight = 1.0 if tag.lower() in _BEHAVIORAL_TAGS else _DOMAIN_WEIGHT_MULTIPLIER
            return tf * idf * weight

        scored = [(tag, df, _tag_score(tag, df)) for tag, df in tag_df.items()]
        supported = [row for row in scored if row[1] >= majority] or scored
        # Support first (shared vocabulary), then domain-weighted TF-IDF.
        supported.sort(key=lambda row: (row[1], row[2]), reverse=True)
        return [row[0] for row in supported], True

    # Tagless cluster: build from text, preferring bigrams over unigrams.
    unigram_df: Counter = Counter()
    bigram_df: Counter = Counter()
    for lesson in cluster_lessons:
        tokens = _extract_text_tokens(lesson.text or "")
        unigram_df.update(set(tokens))
        bigram_df.update(set(_bigrams(tokens)))

    bigrams_supported = [(b, df) for b, df in bigram_df.items() if df >= majority]
    if bigrams_supported:
        bigrams_supported.sort(key=lambda t: t[1], reverse=True)
        return [b for b, _ in bigrams_supported], False

    unigrams_supported = [(u, df) for u, df in unigram_df.items() if df >= majority]
    pool = unigrams_supported or list(unigram_df.items())
    if not pool:
        return [], False
    pool.sort(key=lambda t: t[1], reverse=True)
    return [u for u, _ in pool], False


def deterministic_cluster_label(
    cluster_lessons: list[Lesson],
    total_docs: int,
    global_tag_df: Counter,
) -> str:
    """The always-on, network-free display name for one cluster.

    This is the label floor: synchronous, deterministic, and good enough to
    ship on its own. The async LLM naming pass (``cluster_naming.py``) is an
    upgrade layered on top — it must never block or replace this floor when it
    fails. Reused by the offline label-sanity script and the LLM evidence
    builder so all three see the same candidate ranking.
    """
    terms, _used_tags = _cluster_candidate_terms(cluster_lessons, total_docs, global_tag_df)
    label = _format_cluster_label(terms)
    if label:
        return label
    raw_text = " ".join((lesson.text or "") for lesson in cluster_lessons)[:500].strip()
    return (raw_text[:40] or "Lesson cluster")[:40]


def generate_cluster_labels(tenant: Tenant) -> int:
    """Assign each cluster its hardened deterministic label (tags-first, network-free).

    Curated tags drive the label when present; tagless clusters fall back to
    text bigrams, then unigrams. Majority support kills one-off proper-noun
    leaks and a ~280-word stopword list keeps function/filler words out. The
    result is Title-Case, at most two terms joined with " & ", capped at 40
    chars. An optional async LLM pass may later overwrite these with warmer
    names (see ``cluster_naming.py``); this remains the floor that always runs.
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

        label = deterministic_cluster_label(cluster_lessons, total_docs, global_tag_df)

        Lesson.objects.filter(
            tenant=tenant,
            status="approved",
            cluster_id=cluster_id,
        ).update(cluster_label=label)
        labeled += 1

    return labeled


def compute_positions(tenant: Tenant) -> int:
    """Compute 2D positions from embeddings using PCA + inter-cluster spacing.

    Projects 1536-dim embeddings onto the top 2 principal components via SVD,
    then arranges cluster centroids evenly around a circle so every cluster
    occupies a distinct visual region.  Within each cluster, positions are
    normalised relative to that cluster's own spread so tight or sparse
    clusters both fill their territory equally.

    Falls back to percentile-based normalisation when fewer than 2 clusters
    exist (single-topic tenant or not yet clustered).

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

    # PCA: project onto top 2 principal components
    embeddings = np.array([lesson.embedding for lesson in lessons], dtype=np.float64)
    centered = embeddings - embeddings.mean(axis=0)
    _U, _S, Vt = np.linalg.svd(centered, full_matrices=False)
    projected = centered @ Vt[:2].T  # (n, 2)

    # Group lesson indices by cluster_id
    cluster_to_indices: dict[int | None, list[int]] = {}
    for i, lesson in enumerate(lessons):
        cid = lesson.cluster_id
        cluster_to_indices.setdefault(cid, []).append(i)

    real_clusters = {cid: idx for cid, idx in cluster_to_indices.items() if cid is not None}
    num_clusters = len(real_clusters)

    final_positions = np.zeros((n, 2))

    if num_clusters >= 2:
        # ── Inter-cluster spacing ─────────────────────────────────────────
        # Compute each cluster's PCA centroid.
        cluster_pca_centroids = {cid: projected[np.array(idx)].mean(axis=0) for cid, idx in real_clusters.items()}

        # Arrange cluster centres evenly on a circle, starting from top.
        orbit_radius = 0.62
        sorted_cids = sorted(real_clusters.keys())
        cluster_new_centers: dict[int, np.ndarray] = {}
        for k, cid in enumerate(sorted_cids):
            angle = 2 * math.pi * k / num_clusters - math.pi / 2
            cluster_new_centers[cid] = np.array([orbit_radius * math.cos(angle), orbit_radius * math.sin(angle)])

        # Territory radius shrinks as clusters multiply to avoid overlap.
        territory_radius = max(0.12, 0.38 / math.sqrt(num_clusters))

        # Place each clustered lesson relative to its new cluster centre.
        for cid, indices in real_clusters.items():
            idx_array = np.array(indices)
            pca_centroid = cluster_pca_centroids[cid]
            new_center = cluster_new_centers[cid]
            offsets = projected[idx_array] - pca_centroid  # (k, 2)

            # Scale so the 90th-percentile offset maps to territory_radius.
            magnitudes = np.linalg.norm(offsets, axis=1)
            p90 = float(np.percentile(magnitudes, 90)) if len(magnitudes) > 1 else float(magnitudes[0])
            scale = territory_radius / max(p90, 1e-9)

            for local_i, global_i in enumerate(indices):
                final_positions[global_i] = new_center + offsets[local_i] * scale

        # Place unclustered (noise) lessons near the centre using their raw
        # PCA coordinates scaled to a small inner region.
        if None in cluster_to_indices:
            noise_indices = cluster_to_indices[None]
            noise_pca = projected[np.array(noise_indices)]
            noise_range = float(
                np.percentile(np.abs(noise_pca), 90) if len(noise_indices) > 1 else np.abs(noise_pca).max()
            )
            noise_range = max(noise_range, 1e-9)
            for local_i, global_i in enumerate(noise_indices):
                final_positions[global_i] = np.clip(noise_pca[local_i] / noise_range * 0.20, -0.20, 0.20)

    else:
        # ── Percentile normalisation (0–1 clusters) ───────────────────────
        # Clip outliers at the 95th percentile so a handful of extreme
        # embeddings no longer compress all other lessons into the centre.
        p95 = float(np.percentile(np.abs(projected), 95))
        if p95 > 1e-9:
            final_positions = np.clip(projected / p95, -1.0, 1.0)

    # Safety clip — inter-cluster outliers can slightly exceed ±1.
    final_positions = np.clip(final_positions, -1.0, 1.0)

    updates = []
    for i, lesson in enumerate(lessons):
        lesson.position_x = float(final_positions[i, 0])
        lesson.position_y = float(final_positions[i, 1])
        updates.append(lesson)

    with transaction.atomic():
        Lesson.objects.bulk_update(updates, ["position_x", "position_y"])

    return n


def _enqueue_shared_position_refresh(tenant: Tenant) -> None:
    """Neighborhood PR3 debounce seam: after a recluster moves this tenant's
    star coords, enqueue a coords-only copy-forward onto any sparks they've
    shared to neighbors (``apps.friends.tasks.refresh_shared_positions_task``).

    Lazy + defensive + gated on ``friends_enabled`` — the 99% of tenants without
    the Neighborhood pay nothing, and a friends-side failure must never break the
    core constellation refresh. QStash makes it a debounced async fire (it runs
    inline only in dev/tests where QStash is unconfigured).
    """
    if not getattr(tenant, "friends_enabled", False):
        return
    try:
        from apps.cron.publish import publish_task

        publish_task("refresh_shared_positions", str(tenant.id))
    except Exception:
        logger.exception("shared-position refresh enqueue failed for tenant %s", tenant.id)


def _enqueue_cluster_naming(tenant: Tenant) -> None:
    """Enqueue the async LLM cluster-naming pass after a recluster.

    The deterministic labels are already written by ``generate_cluster_labels``,
    so this is a pure upgrade: a warm LLM name replaces the TF-IDF label when the
    evidence supports it (``apps.lessons.cluster_naming``).

    ASYNC-ONLY — unlike ``_enqueue_shared_position_refresh`` (a pure-DB
    copy-forward that is safe to run inline in the publish_task dev/test
    fallback), naming makes a network + cost LLM call, so we deliberately do NOT
    run it inline: an unconfigured QStash (dev/CI/tests) means SKIP, not a
    synchronous LLM call inside ``refresh_constellation``. Prod (QStash set)
    fires it async on a worker. Gated on the kill-switch and fully defensive —
    a naming-side failure must never break the core constellation refresh.
    """
    if not getattr(settings, "QSTASH_TOKEN", ""):
        return
    if not getattr(settings, "CLUSTER_LABEL_LLM_ENABLED", True):
        return
    try:
        from apps.cron.publish import publish_task

        publish_task("name_clusters", str(tenant.id))
    except Exception:
        logger.exception("cluster-naming enqueue failed for tenant %s", tenant.id)


def refresh_constellation(tenant: Tenant) -> dict[str, object]:
    """Run clustering + labeling + position computation for a tenant."""

    clustering_result = cluster_lessons(tenant)
    label_count = generate_cluster_labels(tenant)
    positions_count = compute_positions(tenant)
    _enqueue_shared_position_refresh(tenant)
    _enqueue_cluster_naming(tenant)
    return {
        **clustering_result,
        "clusters_labeled": label_count,
        "positions_computed": positions_count,
    }
