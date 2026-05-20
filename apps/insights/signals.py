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

This module also exposes ``summarize_topic_signals`` — a lightweight
per-tenant summary used by Horizons' "Topics I've learned" surface
(Phase 3 Day 2). It returns aggregate counts and override status for
every topic the tenant has engaged with. Volatile fields (``latest_z``,
``trend``, ``freshness_days``) are deliberately omitted: the UI shows
meta-state, not values that change by the minute.
"""

from __future__ import annotations

from django.db.models import Count

from apps.journal.models import Document

from .baselines import compute_baseline
from .models import AssistantInsight, PillarSnapshot, TopicRegistry, UserVoicePref


def _summarize_goal(goal) -> str | None:
    """Short summary for the intent block.

    Accepts either a typed ``Goal`` (post-#624) or a legacy ``Document(kind=GOAL)``.
    Prefers the title; falls back to the prose body (``description`` for Goal,
    ``markdown`` for Document).
    """
    if not goal:
        return None
    title = (getattr(goal, "title", "") or "").strip()
    if title and title.lower() not in {"goals", "untitled goal", ""}:
        return title[:200]
    body = (getattr(goal, "description", None) or getattr(goal, "markdown", "") or "").strip()
    if not body:
        return None
    # Drop heading and bullet markers, take first non-empty meaningful line.
    for raw_line in body.splitlines():
        line = raw_line.strip().lstrip("#-* ").strip()
        if line and not line.lower().startswith(("goals", "active", "completed")):
            return line[:200]
    return body[:200]


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
    #
    # Dual-read for the #624 typed-Goal migration: prefer ACTIVE typed Goal
    # rows when present, else the legacy Document(kind=GOAL). Mirrors the
    # pattern in apps/orchestrator/agenda_envelope.py and apps/journal/envelope.py.
    # Local import — see feedback_local_reimport_pattern memory.
    from apps.journal.models import Goal

    topic_goal = (
        Goal.objects.filter(tenant=tenant, pillar=pillar, topic=topic, status=Goal.Status.ACTIVE)
        .order_by("-updated_at")
        .first()
    )
    if topic_goal is None:
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
        Goal.objects.filter(tenant=tenant, pillar=pillar, topic__isnull=True, status=Goal.Status.ACTIVE)
        .order_by("-updated_at")
        .first()
    )
    if pillar_goal is None:
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
    # pref (topic=null). Two-query lookup — single ORDER BY with NULL handling
    # is too fragile across DB backends (PostgreSQL puts NULL FIRST on DESC).
    topic_pref = UserVoicePref.objects.filter(tenant=tenant, pillar=pillar, topic=topic).first()
    pillar_pref = (
        UserVoicePref.objects.filter(tenant=tenant, pillar=pillar, topic__isnull=True).first()
        if topic_pref is None
        else None
    )
    pref = topic_pref or pillar_pref

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


def summarize_topic_signals(tenant) -> list[dict]:
    """Per-tenant summary of every topic the tenant has engaged with.

    Powers Horizons' "Topics I've learned" section (Phase 3 Day 2). The
    payload is intentionally meta-state only: counts of evidence, whether
    a goal exists, and whether the user has set a voice-register override.
    Volatile fields (``latest_z``, ``trend``, ``freshness_days``) belong in
    ``compute_signals`` for the LLM's per-turn judgment, not in a UI that
    re-renders on each Horizons load.

    A topic appears in the result if the tenant has at least one of:
    - a ``PillarSnapshot`` for the topic
    - an ``AssistantInsight`` for the topic
    - a ``Goal`` (typed or legacy Document) tagged to the topic
    - a ``UserVoicePref`` for the topic

    Per-row shape:
        {
          "pillar": "gravity",
          "topic_slug": "dining",
          "topic_display_name": "Dining",
          "sample_size": 8,             # weekly snapshots in last 12w
          "confirmed": 3,               # all-time
          "refuted": 1,                 # all-time
          "has_goal": true,             # typed Goal OR legacy Document
          "register_offset": 1,         # -1 / 0 / +1
          "register_scope": "topic",    # "topic" | "pillar" | null (default)
        }

    Sorted by (pillar, topic_display_name) for stable rendering.
    """
    # PillarSnapshot has no topic FK — sample_size for a topic is derived
    # by running ``compute_baseline`` (which extracts the topic's value out
    # of the snapshot payload via TOPIC_EXTRACTORS). So topic discovery
    # happens in two passes:
    #   1. Tables that carry a topic FK (insights, goals, prefs) — cheap.
    #   2. Topics with extractor support — call compute_baseline to see if
    #      this tenant has any non-null values.
    # Local import for Goal — see feedback_local_reimport_pattern memory.
    from apps.journal.models import Goal

    from .baselines import TOPIC_EXTRACTORS

    insight_topic_ids = set(
        AssistantInsight.objects.filter(tenant=tenant).values_list("topic_id", flat=True).distinct()
    )
    goal_topic_ids = set(
        Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE)
        .exclude(topic__isnull=True)
        .values_list("topic_id", flat=True)
        .distinct()
    )
    legacy_goal_topic_ids = set(
        Document.objects.filter(tenant=tenant, kind=Document.Kind.GOAL)
        .exclude(topic__isnull=True)
        .values_list("topic_id", flat=True)
        .distinct()
    )
    pref_topic_ids = set(
        UserVoicePref.objects.filter(tenant=tenant)
        .exclude(topic__isnull=True)
        .values_list("topic_id", flat=True)
        .distinct()
    )

    fk_touched = insight_topic_ids | goal_topic_ids | legacy_goal_topic_ids | pref_topic_ids

    # Pre-fetch all topics that *could* have snapshot data via extractors,
    # then run compute_baseline per (pillar, slug) to determine sample_size.
    extractor_pairs: list[tuple[str, str]] = [
        (pillar, slug) for pillar, slugs in TOPIC_EXTRACTORS.items() for slug in slugs
    ]
    extractor_topics = {
        (t.pillar, t.slug): t
        for t in TopicRegistry.objects.filter(
            pillar__in={p for p, _ in extractor_pairs},
            slug__in={s for _, s in extractor_pairs},
        )
    }

    sample_sizes: dict[int, int] = {}
    snapshot_touched: set = set()
    for pillar, slug in extractor_pairs:
        topic = extractor_topics.get((pillar, slug))
        if topic is None:
            continue
        baseline = compute_baseline(tenant=tenant, pillar=pillar, topic_slug=slug)
        size = int(baseline.get("sample_size", 0) or 0)
        if size > 0:
            sample_sizes[topic.id] = size
            snapshot_touched.add(topic.id)

    touched_topic_ids = fk_touched | snapshot_touched
    if not touched_topic_ids:
        return []

    topics = list(TopicRegistry.objects.filter(id__in=touched_topic_ids).order_by("pillar", "display_name"))
    if not topics:
        return []

    # Bulk-load insight status counts so we don't N+1 the DB.
    insight_counts: dict[tuple, int] = {}
    for row in (
        AssistantInsight.objects.filter(tenant=tenant, topic_id__in=touched_topic_ids)
        .values("topic_id", "status")
        .annotate(n=Count("id"))
    ):
        insight_counts[(row["topic_id"], row["status"])] = row["n"]

    # Voice prefs: bulk-load topic + pillar-wide, resolve per-topic in Python.
    topic_prefs = {
        pref.topic_id: pref for pref in UserVoicePref.objects.filter(tenant=tenant, topic_id__in=touched_topic_ids)
    }
    pillar_prefs = {pref.pillar: pref for pref in UserVoicePref.objects.filter(tenant=tenant, topic__isnull=True)}

    rows: list[dict] = []
    for topic in topics:
        topic_pref = topic_prefs.get(topic.id)
        pillar_pref = pillar_prefs.get(topic.pillar)
        if topic_pref is not None:
            offset = topic_pref.register_offset
            scope: str | None = "topic"
        elif pillar_pref is not None:
            offset = pillar_pref.register_offset
            scope = "pillar"
        else:
            offset = 0
            scope = None

        rows.append(
            {
                "pillar": topic.pillar,
                "topic_slug": topic.slug,
                "topic_display_name": topic.display_name,
                "sample_size": sample_sizes.get(topic.id, 0),
                "confirmed": int(insight_counts.get((topic.id, AssistantInsight.Status.CONFIRMED), 0)),
                "refuted": int(insight_counts.get((topic.id, AssistantInsight.Status.REFUTED), 0)),
                "has_goal": (topic.id in goal_topic_ids) or (topic.id in legacy_goal_topic_ids),
                "register_offset": offset,
                "register_scope": scope,
            }
        )
    return rows
