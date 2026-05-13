"""Resolve a candidate topic string to a TopicRegistry row.

Resolution order:

1. Exact slug match on a canonical topic.
2. Exact match on a TopicAlias (case- and whitespace-insensitive).
3. Embedding similarity above threshold — TODO, deferred to a follow-up. The
   pgvector extension is available; we still need to wire up an embedding
   source for canonical names and aliases before this layer can land.
4. Insert a new topic with ``status=proposed`` and return it.

Always returns a TopicRegistry row — never None.
"""

from __future__ import annotations

import re
import unicodedata

from django.db import transaction

from .models import TopicAlias, TopicRegistry


def _normalize(s: str) -> str:
    """Lowercase, strip, collapse internal whitespace, NFKC normalize."""
    s = unicodedata.normalize("NFKC", s).strip().lower()
    return re.sub(r"\s+", " ", s)


def _slugify(s: str) -> str:
    """Convert a normalized string into a topic slug matching seed style."""
    s = _normalize(s)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "untitled"


def _make_unique_slug(pillar: str, base_slug: str) -> str:
    slug = base_slug
    suffix = 2
    while TopicRegistry.objects.filter(pillar=pillar, slug=slug).exists():
        slug = f"{base_slug}_{suffix}"
        suffix += 1
    return slug


@transaction.atomic
def resolve_topic(
    pillar: str,
    candidate: str,
    *,
    model_version: str = "",
) -> TopicRegistry:
    """Resolve ``candidate`` to a TopicRegistry row, creating a proposed entry if needed."""
    candidate_norm = _normalize(candidate)
    slug_candidate = _slugify(candidate_norm)

    canonical = TopicRegistry.objects.filter(
        pillar=pillar,
        slug=slug_candidate,
        status=TopicRegistry.Status.CANONICAL,
    ).first()
    if canonical:
        return canonical

    alias = (
        TopicAlias.objects.filter(
            topic__pillar=pillar,
            topic__status=TopicRegistry.Status.CANONICAL,
            alias__iexact=candidate_norm,
        )
        .select_related("topic")
        .first()
    )
    if alias:
        return alias.topic

    return TopicRegistry.objects.create(
        pillar=pillar,
        slug=_make_unique_slug(pillar, slug_candidate),
        display_name=candidate_norm or candidate,
        status=TopicRegistry.Status.PROPOSED,
        source=TopicRegistry.Source.PROPOSED_BY_MODEL,
        proposed_by_model_version=model_version,
    )
