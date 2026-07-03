"""Deterministic, speech-safe "right now" status for a Siri status ask.

The Siri status endpoint (:class:`apps.router.siri_views.SiriQuickStatusView`)
also returns ``snapshot_md`` — the LLM-facing context digest
(``render_context_digest``). That digest is written for a *model* to read: it
carries markdown headings, bold markers, internal directives ("_Read this
before composing any proactive turn_"), and literal tool-call instructions
(``nbhd_goal_list({status: 'active'})``). Handed to Siri's TTS it is spoken
aloud verbatim — the markdown and tool names come out as garbled nonsense, and
the internal directives leak the assistant's private reasoning scaffold to the
user.

This module composes a SEPARATE ``spoken`` string that is safe to speak:

* Built **deterministically** from the same structured pillar sources the
  digest's sections query (typed ``Goal`` / ``Task`` / ``Workout`` /
  ``PayoffPlan`` / ``Purpose`` rows) — NOT by parsing the rendered markdown, and
  never by calling a model. It stays Tier 0.
* Plain spoken English: no markdown, no symbols, no code, no tool names, no
  internal directives. Only counts and a couple of boolean facts — nothing that
  needs PII rehydration, so no real names/titles are ever spoken.
* Feature-gated exactly like the envelope sections: Fuel on ``fuel_enabled``,
  Core on ``core_enabled``, Finance on ``finance_active`` (which folds in the
  platform ``GRAVITY_ENABLED`` kill switch).
* Empty sections are omitted; the whole thing is hard-capped so a spoken turn
  stays short.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Spoken utterances should stay short — Siri reads this aloud. A couple of
# sentences of counts is plenty; anything longer is a wall of numbers.
SPOKEN_MAX_CHARS = 280

# What the assistant says when nothing meaningful is pending — better than
# silence (which reads as a failure) or an empty string.
_ALL_CLEAR = "You're all caught up. Nothing pressing right now."


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    """``1 -> "1 task"``, ``3 -> "3 tasks"`` — digits are fine for speech."""
    word = singular if n == 1 else (plural or singular + "s")
    return f"{n} {word}"


def _join_clause(parts: list[str]) -> str:
    """Natural-language list join: ``[a] -> "a"``, ``[a, b] -> "a and b"``,
    ``[a, b, c] -> "a, b, and c"``."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _fit(sentences: list[str], *, max_chars: int) -> str:
    """Join sentences with single spaces, dropping trailing ones that would
    push the total past ``max_chars``. Priority order is the caller's list
    order, so lower-value sentences fall off the end first."""
    out: list[str] = []
    total = 0
    for s in sentences:
        add = len(s) + (1 if out else 0)
        if total + add > max_chars:
            break
        out.append(s)
        total += add
    return " ".join(out)


def compose_spoken_status(tenant) -> str:
    """Return a 1-3 sentence, speech-safe summary of the tenant's state.

    Pure w.r.t. the DB (reads only) and never calls a model. Each pillar is
    isolated in its own guard so one failing query degrades to omitting that
    pillar rather than blanking the whole utterance — mirroring the
    per-section resilience in ``render_context_digest``.
    """
    from apps.common.tenant_tz import tenant_today

    try:
        today = tenant_today(tenant)
    except Exception:
        from datetime import date

        today = date.today()

    sentences: list[str] = []

    # ── Tasks + goals — the load-bearing "what's on my plate" line ──────────
    # Combined into one "You have ..." sentence (matches the natural spoken
    # shape "You have 13 open tasks and 2 active goals").
    try:
        from datetime import timedelta

        from apps.journal.models import Goal, Task

        open_count = Task.objects.filter(tenant=tenant, status=Task.Status.OPEN).count()
        in_progress_count = Task.objects.filter(tenant=tenant, status=Task.Status.IN_PROGRESS).count()
        due_soon = (
            Task.objects.filter(
                tenant=tenant,
                status=Task.Status.OPEN,
                due_date__isnull=False,
                due_date__lte=today + timedelta(days=7),
            ).count()
            if open_count
            else 0
        )
        active_goals = Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE).count()

        parts: list[str] = []
        if open_count:
            parts.append(_plural(open_count, "open task"))
        if in_progress_count:
            parts.append(f"{in_progress_count} in progress")
        if active_goals:
            parts.append(_plural(active_goals, "active goal"))
        if parts:
            sentences.append(f"You have {_join_clause(parts)}.")
        if due_soon:
            verb = "is" if due_soon == 1 else "are"
            sentences.append(f"{due_soon} {verb} due this week.")
    except Exception:
        logger.warning("siri spoken: tasks/goals section failed", exc_info=True)

    # ── Fuel — planned sessions coming up ──────────────────────────────────
    if getattr(tenant, "fuel_enabled", False):
        try:
            from datetime import timedelta

            from apps.fuel.models import Workout, WorkoutStatus

            planned = Workout.objects.filter(
                tenant=tenant,
                status=WorkoutStatus.PLANNED,
                date__gte=today,
                date__lte=today + timedelta(days=7),
            ).count()
            if planned:
                sentences.append(f"{_plural(planned, 'workout')} planned this week.")
        except Exception:
            logger.warning("siri spoken: fuel section failed", exc_info=True)

    # ── Core — mindfulness sessions this week ──────────────────────────────
    if getattr(tenant, "core_enabled", False):
        try:
            from datetime import timedelta

            from apps.core.models import MeditationSession, MeditationStatus

            count_7d = MeditationSession.objects.filter(
                tenant=tenant,
                status=MeditationStatus.READY,
                date__gte=today - timedelta(days=7),
            ).count()
            if count_7d:
                sentences.append(f"{_plural(count_7d, 'meditation')} this week.")
        except Exception:
            logger.warning("siri spoken: core section failed", exc_info=True)

    # ── Finance — payoff plan active (no dollar amounts spoken) ────────────
    if getattr(tenant, "finance_active", False):
        try:
            from apps.finance.models import PayoffPlan

            if PayoffPlan.objects.filter(tenant=tenant, is_active=True).exists():
                sentences.append("Your payoff plan is active.")
        except Exception:
            logger.warning("siri spoken: finance section failed", exc_info=True)

    # ── North Star — a confirmed purpose is set (statement not spoken) ─────
    try:
        from apps.journal.models import Purpose

        has_north_star = Purpose.objects.filter(
            tenant=tenant,
            status__in=[Purpose.Status.CONFIRMED, Purpose.Status.EVOLVING],
        ).exists()
        if has_north_star:
            sentences.append("Your North Star is set.")
    except Exception:
        logger.warning("siri spoken: north-star section failed", exc_info=True)

    if not sentences:
        return _ALL_CLEAR

    return _fit(sentences, max_chars=SPOKEN_MAX_CHARS)
