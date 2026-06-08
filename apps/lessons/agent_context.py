"""Assistant-facing constellation context.

The constellation — a galaxy of :class:`~apps.lessons.models.Lesson` "stars" —
accumulates rich, user-authored context that the journal / fuel / goals pillars
already feed to the assistant but the constellation never did:

* ``galaxy_note``      — the player's pinned note on a star
* ``StarJournalEntry`` — star-scoped reflections written while tutoring/revisiting
* ``TutoringSession``  — the honest signals the assistant captured while exploring
  a star with the user (did they restate it accurately, find edge cases, make
  connections, achieve mastery, drift to another topic)

Until now those were write-mostly: surfaced in the constellation UI but never
read back by the assistant. This module is the single place that shapes that
enriched context, so the three surfaces that expose it never drift:

* USER.md envelope section            -> :mod:`apps.lessons.envelope`
* ``nbhd_journal_context`` (session init) -> ``RuntimeJournalContextView``
* ``nbhd_constellation_notes`` (pull)     -> ``RuntimeConstellationNotesView``

Design note: the backend assembles raw signals (notes, reflections, the model's
own judgments from tutoring) and hands them to the assistant to weigh — it does
not pre-score "relevance" into a formula. See ``feedback_llm_not_formula_for_judgment``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db.models import Count, Q
from django.utils import timezone

from apps.lessons.models import Lesson
from apps.tenants.models import Tenant

# Default lookback for "what has the user been working through lately".
_DEFAULT_WINDOW_DAYS = 30

# Human-readable star lifecycle labels for the assistant (vs. the raw enum).
_STAGE_LABEL = {
    "proto": "proto-star",
    "ignited": "ignited",
    "radiant": "radiant",
    "supernova": "supernova",
}


def _truncate(text: str | None, cap: int) -> str:
    """Collapse whitespace and hard-cap ``text`` to ``cap`` chars with an ellipsis."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= cap:
        return collapsed
    return collapsed[: cap - 1].rstrip() + "…"


def _star_activity_at(star: Lesson) -> datetime:
    """Most-recent engagement timestamp for a star, for recency ordering.

    Considers tutoring, galaxy visits, the newest star-journal entry, and
    finally approval/creation so a star carrying only a pinned note still sorts
    deterministically. Sorting in Python (not SQL) sidesteps the Postgres
    ``NULL FIRST on DESC`` trap — see ``feedback_postgres_null_first_desc``.

    Relies on ``journal_entries`` being prefetched by :func:`recent_active_stars`;
    falls back to a query for single-star callers.
    """
    candidates: list[datetime] = []
    if star.last_tutored_at is not None:
        candidates.append(star.last_tutored_at)
    if star.last_visited_at is not None:
        candidates.append(star.last_visited_at)
    entries = list(star.journal_entries.all())
    if entries:
        candidates.append(max(e.created_at for e in entries))
    candidates.append(star.approved_at or star.created_at)
    return max(candidates)


def recent_active_stars(tenant: Tenant, *, days: int = _DEFAULT_WINDOW_DAYS, limit: int = 5) -> list[Lesson]:
    """Approved stars the user has been actively working through, newest first.

    A star is "active" when, within the window, it was tutored, visited, or
    journaled on — or, at any time, has a pinned ``galaxy_note`` (a deliberate,
    persistent piece of context the user chose to surface).
    """
    cutoff = timezone.now() - timedelta(days=days)
    qs = (
        Lesson.objects.filter(tenant=tenant, status="approved")
        .filter(
            Q(galaxy_note__gt="")
            | Q(last_tutored_at__gte=cutoff)
            | Q(last_visited_at__gte=cutoff)
            | Q(journal_entries__created_at__gte=cutoff)
            | Q(tutoring_sessions__created_at__gte=cutoff)
        )
        .distinct()
        .prefetch_related("journal_entries", "tutoring_sessions")
    )
    stars = list(qs)
    stars.sort(key=_star_activity_at, reverse=True)
    return stars[:limit]


def _compact_insight(session) -> dict[str, Any]:
    """The honest signals from one tutoring session — the model's own judgments.

    These are explicit assistant judgments captured during play (not backend
    proxies): they tell a later assistant turn how the user engaged with this
    star so it can teach to their actual strengths and blind spots.
    """
    return {
        "phases_completed": list(session.phases_completed or []),
        "restated_accurately": session.player_restated_accurately,
        "found_edge_cases": session.player_found_edge_cases,
        "connections_made": len(session.connections_made or []),
        "topic_shifted": session.topic_shifted or "",
        "mastery_achieved": session.mastery_achieved,
        "created_at": session.created_at.isoformat(),
    }


def build_star_context(star: Lesson, *, full: bool = False) -> dict[str, Any]:
    """Shape one star's enriched context for the assistant.

    ``full=True`` (the pull tool drilling into a star) keeps more journal
    entries / tutoring sessions and longer excerpts; the default (compact) form
    is sized for always-on session context.
    """
    journal_limit = 5 if full else 2
    tutoring_limit = 5 if full else 2
    text_cap = 400 if full else 180

    entries = sorted(star.journal_entries.all(), key=lambda e: e.created_at, reverse=True)[:journal_limit]
    sessions = sorted(star.tutoring_sessions.all(), key=lambda s: s.created_at, reverse=True)[:tutoring_limit]

    return {
        "id": star.id,
        "text": star.text,
        "stage": _STAGE_LABEL.get(star.star_stage, star.star_stage),
        "galaxy_note": star.galaxy_note or "",
        "tags": list(star.tags or []),
        "cluster_label": star.cluster_label or "",
        "tutoring_sessions_count": star.tutoring_sessions_count,
        "last_tutored_at": star.last_tutored_at.isoformat() if star.last_tutored_at else None,
        "last_visited_at": star.last_visited_at.isoformat() if star.last_visited_at else None,
        "journal_entries": [
            {
                "text": _truncate(e.text, text_cap),
                "entry_type": e.entry_type,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ],
        "tutoring_insights": [_compact_insight(s) for s in sessions],
    }


def _galaxy_summary(tenant: Tenant) -> dict[str, Any]:
    """Lightweight totals so the assistant has a sense of the whole galaxy."""
    rows = Lesson.objects.filter(tenant=tenant, status="approved").values("star_stage").annotate(n=Count("id"))
    by_stage = {row["star_stage"]: row["n"] for row in rows}
    return {
        "total_stars": sum(by_stage.values()),
        "by_stage": by_stage,
    }


def build_constellation_context(tenant: Tenant, *, days: int = _DEFAULT_WINDOW_DAYS, limit: int = 5) -> dict[str, Any]:
    """Proactive constellation bundle for ``nbhd_journal_context`` (session init).

    Returns ``{}`` when there's no recent activity so the session-init payload
    omits the key entirely — mirroring how the backbone omits empty goals/tasks.
    """
    stars = recent_active_stars(tenant, days=days, limit=limit)
    if not stars:
        return {}
    return {
        "active_stars": [build_star_context(star) for star in stars],
        "summary": _galaxy_summary(tenant),
    }


def constellation_notes_payload(
    tenant: Tenant,
    *,
    q: str | None = None,
    star_id: int | None = None,
    limit: int = 5,
    days: int = _DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Full payload for the ``nbhd_constellation_notes`` pull tool.

    Three modes, in precedence order:
      * ``star_id`` — full context for one approved star
      * ``q``       — semantic/text search over stars, each enriched
      * default     — recently active stars
    """
    if star_id is not None:
        star = Lesson.objects.filter(tenant=tenant, status="approved", id=star_id).first()
        stars = [star] if star is not None else []
        mode = "star"
    elif q:
        # Local import keeps the embedding/search stack out of module import.
        from apps.lessons.services import search_lessons

        stars = list(search_lessons(tenant=tenant, query=q, limit=limit))
        mode = "search"
    else:
        stars = recent_active_stars(tenant, days=days, limit=limit)
        mode = "recent"

    return {
        "mode": mode,
        "count": len(stars),
        "stars": [build_star_context(star, full=True) for star in stars],
        "summary": _galaxy_summary(tenant),
    }


# ---------------------------------------------------------------------------
# USER.md envelope rendering (always-on context)
# ---------------------------------------------------------------------------


def _envelope_insight_line(insights: list[dict[str, Any]]) -> str:
    """One short line summarising the latest tutoring signal, or empty string."""
    if not insights:
        return ""
    latest = insights[0]
    bits: list[str] = []
    if latest.get("mastery_achieved"):
        bits.append("reached mastery")
    if latest.get("found_edge_cases"):
        bits.append("found edge cases")
    if latest.get("restated_accurately") is False:
        bits.append("struggled to restate it")
    if latest.get("connections_made"):
        bits.append(f"linked {latest['connections_made']} other star(s)")
    if latest.get("topic_shifted"):
        bits.append(f"drifted toward {latest['topic_shifted']}")
    if not bits:
        return ""
    return "tutoring: " + ", ".join(bits)


def render_constellation_envelope(tenant: Tenant, *, limit: int = 3) -> str:
    """Markdown body for the USER.md ``Constellation — active stars`` section.

    Empty string when nothing is active, so the registry omits the heading.
    Kept tight — this rides in USER.md on every turn.
    """
    stars = recent_active_stars(tenant, days=_DEFAULT_WINDOW_DAYS, limit=limit)
    if not stars:
        return ""

    lines: list[str] = []
    for star in stars:
        ctx = build_star_context(star)
        first_line = _truncate(star.text, 120)
        lines.append(f"- **{first_line}** _({ctx['stage']})_")
        if ctx["galaxy_note"]:
            lines.append(f"  - pinned note: {_truncate(ctx['galaxy_note'], 160)}")
        if ctx["journal_entries"]:
            lines.append(f"  - latest reflection: {_truncate(ctx['journal_entries'][0]['text'], 160)}")
        insight = _envelope_insight_line(ctx["tutoring_insights"])
        if insight:
            lines.append(f"  - {insight}")
    return "\n".join(lines)
