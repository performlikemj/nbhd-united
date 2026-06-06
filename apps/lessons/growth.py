"""Star growth — promote a star's stage as the player engages with it.

A star (lesson) grows proto → ignited → radiant → supernova as the user does
deliberate work on it: writing notes, drawing connections, and tutoring sessions.

This is the SINGLE source of truth for the ladder. Every path feeds it:
  * **Tutoring** — ``tutoring.end_tutoring`` increments the session count, then
    calls ``apply_star_growth``.
  * **Cheap signals** — a note added (``star_journal_create``) or a connection
    made (``connect``) call ``apply_star_growth`` directly.

``apply_star_growth`` only ever **promotes** — a star never shrinks. It's
monotonic, so paths can't disagree into a flip-flop, and "stars grow, they don't
un-grow" holds. A bare visit does NOT grow a star — growth is earned by doing
something, not flying past.
"""

from __future__ import annotations

from .models import Lesson

# The ladder, low → high. Used to enforce monotonic promotion.
STAGE_RANK = {"proto": 0, "ignited": 1, "radiant": 2, "supernova": 3}


def compute_star_stage(star: Lesson) -> str:
    """Stage implied by a star's *current* engagement (notes + connections +
    tutoring). No "+1" — operates on persisted counts; returns ``proto`` for an
    untouched star.
    """
    sessions = star.tutoring_sessions_count or 0
    notes = star.journal_entries.count()
    connections = star.connections_out.count()
    # Tutoring is the deepest signal, then notes, then connections. The combined
    # score lets a mix of engagement add up to the next tier.
    score = sessions * 3 + notes * 1.5 + connections

    if sessions >= 8 or notes >= 8 or score >= 14:
        return "supernova"
    if sessions >= 3 or notes >= 3 or connections >= 5 or score >= 5:
        return "radiant"
    if sessions >= 1 or notes >= 1 or connections >= 1:
        return "ignited"
    return "proto"


def apply_star_growth(star: Lesson) -> str:
    """Recompute and persist the star's stage, **promoting only** (never demote).

    Returns the star's stage after the (possible) promotion. Saves just the one
    field when it changes; a no-op otherwise.
    """
    computed = compute_star_stage(star)
    if STAGE_RANK.get(computed, 0) > STAGE_RANK.get(star.star_stage, 0):
        star.star_stage = computed
        star.save(update_fields=["star_stage"])
    return star.star_stage
