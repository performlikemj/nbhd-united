"""Structured signals for the LLM to judge voice register per topic.

This is the inputs side of Phase 3's graduated-voice model. The backend returns
*raw signals* — data state, calibration counts, intent presence, user override
status, hard-floor flags — and the LLM weighs them against live conversation
context to pick a register. No score is computed. No register is chosen here.

Why no score: a deterministic confidence score conflates *evidential basis*
(do I have enough data?) with *prescriptive appropriateness* (should I be
direct with this user right now?). Those are different problems; the LLM is
better positioned to make the second call with full conversation context.
Backend's job is to surface what's true.

Hard floors are the only mechanical constraints the LLM is bound by:
``can_be_direct`` requires ≥4 snapshots; ``can_exceed_observation`` requires
≥3 total user responses (confirmed + refuted). The LLM can choose a register
*lower* than the floors permit, but not higher. User-explicit overrides
(``register_offset`` in UserVoicePref) are separate — they're stored
permission, not LLM inference.
"""

from __future__ import annotations

from django.db.models import Count, Q

from apps.journal.models import Document

from .baselines import compute_baseline
from .models import AssistantInsight, PillarSnapshot, TopicRegistry, UserVoicePref


def _summarize_goal(doc: Document) -> str | None:
    """Extract a short summary for the intent block (title preferred, else markdown prefix)."""
    if not doc:
        return None
    title = (doc.title or "").strip()
    if title and title.lower() not in {"goals", "untitled goal", ""}:
        return title[:200]
    markdown = (doc.markdown or "").strip()
    if not markdown:
        return None
    # Drop heading and bullet markers, take first non-empty meaningful line.
    for raw_line in markdown.splitlines():
        line = raw_line.strip().lstrip("#-* ").strip()
        if line and not line.lower().startswith(("goals", "active", "completed")):
            return line[:200]
    return markdown[:200]


def compute_signals(
    *,
    tenant,
    pillar: str,
    topic_slug: str,
    window_weeks: int = 12,
    granularity: str = PillarSnapshot.Granularity.WEEKLY,
) -> dict:
    """Return structured signals for ``(tenant, pillar, topic_slug)``.

    Output is consumed by the LLM via the ``nbhd_insights_signals`` plugin
    tool. Schema documented inline below the function.
    """
    topic = TopicRegistry.objects.filter(pillar=pillar, slug=topic_slug).first()

    if topic is None:
        # Slug isn't in the registry yet — return a stub the LLM can detect.
        # The assistant should respond by either choosing a canonical slug,
        # passing a natural string through nbhd_insights_record (which will
        # auto-propose), or falling back to nbhd_insights_history.
        return {
            "pillar": pillar,
            "topic_slug": topic_slug,
            "topic_display_name": None,
            "topic_known": False,
            "data": None,
            "calibration": None,
            "intent": None,
            "user_voice_pref": None,
            "hard_floors": {
                "can_be_direct": False,
                "can_exceed_observation": False,
                "reason": "topic_unknown",
            },
        }

    baseline = compute_baseline(
        tenant=tenant,
        pillar=pillar,
        topic_slug=topic_slug,
        window_weeks=window_weeks,
        granularity=granularity,
    )

    counts_by_status = {
        row["status"]: row["n"]
        for row in (
            AssistantInsight.objects.filter(tenant=tenant, pillar=pillar, topic=topic)
            .values("status")
            .annotate(n=Count("id"))
        )
    }
    confirmed = int(counts_by_status.get(AssistantInsight.Status.CONFIRMED, 0))
    refuted = int(counts_by_status.get(AssistantInsight.Status.REFUTED, 0))
    open_count = int(counts_by_status.get(AssistantInsight.Status.OPEN, 0))
    response_total = confirmed + refuted

    # Intent: prefer a goal tagged to this exact topic; fall back to a
    # pillar-tagged goal (untagged-pillar goals don't count — they predate
    # Phase 0 tagging and might not be about this pillar at all).
    topic_goal = (
        Document.objects.filter(
            tenant=tenant,
            kind=Document.Kind.GOAL,
            pillar=pillar,
            topic=topic,
        )
        .order_by("-updated_at")
        .first()
    )
    pillar_goal = (
        Document.objects.filter(
            tenant=tenant,
            kind=Document.Kind.GOAL,
            pillar=pillar,
            topic__isnull=True,
        )
        .order_by("-updated_at")
        .first()
    )
    chosen_goal = topic_goal or pillar_goal

    # Voice pref: prefer a topic-specific pref; fall back to a pillar-wide
    # pref (topic=null). Both stored in the same table.
    pref = (
        UserVoicePref.objects.filter(tenant=tenant, pillar=pillar)
        .filter(Q(topic=topic) | Q(topic__isnull=True))
        .order_by("-topic_id")  # topic-specific first; null topic_id sorts last
        .first()
    )

    sample_size = baseline.get("sample_size", 0) or 0

    return {
        "pillar": pillar,
        "topic_slug": topic.slug,
        "topic_display_name": topic.display_name,
        "topic_known": True,
        "data": {
            "supported": baseline.get("supported", False),
            "sample_size": sample_size,
            "latest_value": baseline.get("latest"),
            "mean": baseline.get("mean"),
            "stdev": baseline.get("stdev"),
            "latest_z": baseline.get("latest_z"),
            "trend_per_window_step": baseline.get("trend"),
            "freshness_days": baseline.get("freshness_days"),
            "granularity": granularity,
            "window_weeks": window_weeks,
        },
        "calibration": {
            "confirmed": confirmed,
            "refuted": refuted,
            "open": open_count,
            "response_total": response_total,
            # ratio is null when no user responses exist — the LLM should
            # treat that as "no calibration yet," not "0% confirmed."
            "ratio": round(confirmed / response_total, 4) if response_total > 0 else None,
        },
        "intent": {
            "has_stated_goal": chosen_goal is not None,
            "goal_scope": ("topic" if topic_goal else ("pillar" if pillar_goal else None)),
            "goal_summary": _summarize_goal(chosen_goal),
        },
        "user_voice_pref": (
            {
                "register_offset": pref.register_offset,
                "tone": pref.tone,
                "volume": pref.volume,
                "scope": "topic" if pref.topic_id else "pillar",
                "updated_at": pref.updated_at.isoformat(),
            }
            if pref is not None
            else {
                "register_offset": 0,
                "tone": UserVoicePref.Tone.GENTLE.value,
                "volume": UserVoicePref.Volume.WEEKLY.value,
                "scope": None,
                "updated_at": None,
            }
        ),
        "hard_floors": {
            # Mechanical safety rails. The LLM is bound by these — it cannot
            # choose a register hotter than the floors permit, regardless of
            # how the rest of the signals read. Only an explicit user
            # override (stored offset) can lift them.
            "can_be_direct": sample_size >= 4,
            "can_exceed_observation": response_total >= 3,
        },
    }
