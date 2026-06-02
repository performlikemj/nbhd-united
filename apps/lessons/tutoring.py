"""Tutoring service — Claude-style tutor pattern for star landing sessions.

A tutoring session walks the player through 5 phases with their own learning:
  1. Restate    — can they explain the lesson in their own words?
  2. Deepen     — do they understand WHY it works?
  3. Stress-test — can they find edge cases and exceptions?
  4. Connect    — can they link it to other things they know?
  5. Apply      — can they transfer the lesson to a current situation?

The AI tutor is curious and supportive, not didactic. It learns from how
the player responds — what sticks, where the blind spots are, what they
value — and stores signals in the TutoringSession model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests
from django.conf import settings
from django.utils import timezone

from .models import Lesson, TutoringSession

PHASES = ["restate", "deepen", "stress_test", "connect", "apply"]

TUTORING_MODEL = "anthropic/claude-sonnet-4.6"

# ---------------------------------------------------------------------------
# System prompt for the AI tutor
# ---------------------------------------------------------------------------

TUTOR_SYSTEM = """You are a curious, thoughtful tutor helping someone explore their own learning. 
You are not a lecturer — you are a guide who genuinely wants to understand how this person thinks.

The user has a collection of personal lessons — things they've learned from their own life. 
Your job is to help them deepen their understanding of one of those lessons through 
a structured but natural conversation.

**Your persona:**
- Curious, not didactic. Ask questions because you're genuinely interested.
- Warm but not performative. "That's interesting — tell me more" not "AMAZING INSIGHT!!! 🎉"
- Push gently on edge cases but don't interrogate.
- Celebrate connections and insights — briefly, authentically.
- If the player struggles, help them find the answer rather than giving it.
- Reference the player's own journal entries (if provided) to make it personal.

**The five phases:**
1. **Restate** — Ask the player to explain the lesson in their own words. 
   Verify they actually understand it, not just recall it.
2. **Deepen** — Explore the "why" behind the lesson. What's the mechanism? 
   Why does this work? What's the deeper principle?
3. **Stress-test** — Challenge the lesson. When would this NOT apply? 
   What are the edge cases? Is there ever a reason to break this rule?
4. **Connect** — Help the player draw links to other knowledge. 
   Does this connect to anything else they know — in the same domain or outside it?
5. **Apply** — Test transfer. "You're facing [current situation]. How does this lesson adapt?"

**Phase transition rules:**
- Complete each phase before moving on (don't skip ahead).
- Move on when the player demonstrates genuine understanding AND engagement.
- If a player gives a one-word answer, probe gently: "Can you say a bit more about that?"
- The player CAN skip a phase by saying "let's move on" or similar — respect that.

**End the session:**
- After Apply is complete (or player exits), summarize briefly what was covered.
- Mention one thing that stood out about how the player engaged with this lesson.
- Don't assign homework or make promises. Just close naturally.

**CRITICAL — Format your response as JSON:**
Reply with a JSON object: {"text": "your message to the player", "current_phase": "deepen", "phase_complete": true}

- `text`: your conversational message to the player (keep this warm and natural)
- `current_phase`: which phase you're currently in (one of: restate, deepen, stress_test, connect, apply)
- `phase_complete`: true if the current phase has been completed and you're ready to move to the next one
- `session_complete`: (optional) true after Apply is done — the session should end
- `connections_found`: (optional) list of lesson IDs the player connected to
- `topic_shifted`: (optional) what the player drifted toward if not the lesson topic"""


def _resolve_api_key() -> str:
    key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError("OPENROUTER_API_KEY is not configured")
    return key


def _tutor_request(messages: list[dict], temperature: float = 0.7) -> dict[str, Any]:
    """Make a tutoring LLM call and return the parsed JSON response."""
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {_resolve_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": TUTORING_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return __import__("json").loads(content)


@dataclass
class TutoringState:
    """Mutable state for an active tutoring session."""

    session_id: str
    star: Lesson
    messages: list[dict] = field(default_factory=list)
    current_phase: str = "restate"
    phases_completed: list[str] = field(default_factory=list)
    connections_found: list[int] = field(default_factory=list)
    player_restated_accurately: bool | None = None
    player_found_edge_cases: bool | None = None
    topic_shifted: str = ""
    mastery_achieved: bool = False
    skipped: bool = False

    def phase_index(self) -> int:
        try:
            return PHASES.index(self.current_phase)
        except ValueError:
            return 0

    def next_phase(self) -> str | None:
        idx = self.phase_index()
        if idx + 1 < len(PHASES):
            return PHASES[idx + 1]
        return None

    def advance_phase(self) -> bool:
        nxt = self.next_phase()
        if nxt:
            self.phases_completed.append(self.current_phase)
            self.current_phase = nxt
            return True
        return False


# ---------------------------------------------------------------------------
# In-memory session store (for MVP — replace with cache/DB for production)
# ---------------------------------------------------------------------------

_sessions: dict[str, TutoringState] = {}


def start_tutoring(star: Lesson) -> dict[str, Any]:
    """Begin a new tutoring session for a star.

    Returns the first tutor message and session metadata.
    """
    import uuid as _uuid

    session_id = str(_uuid.uuid4())
    state = TutoringState(session_id=session_id, star=star)

    # Build journal context if the star has journal entries
    journal_context = _build_journal_context(star)

    # Phase 1 prompt: Restate
    phase_prompt = _build_phase_prompt(state, journal_context)

    system_msg = {"role": "system", "content": TUTOR_SYSTEM}
    user_msg = {"role": "user", "content": phase_prompt}

    state.messages = [system_msg, user_msg]
    _sessions[session_id] = state

    try:
        result = _tutor_request(state.messages)
    except Exception:
        # Fallback — don't crash on LLM failure
        result = {
            "text": f"Let's explore something you learned: *{star.text[:100]}{'...' if len(star.text) > 100 else ''}*\n\nCan you explain this in your own words?",
            "current_phase": "restate",
            "phase_complete": False,
        }

    msg = result.get("text", "")
    current_phase = result.get("current_phase", "restate")

    # Track the assistant message
    state.messages.append({"role": "assistant", "content": msg})

    return {
        "session_id": session_id,
        "message": msg,
        "current_phase": current_phase,
        "phase_index": state.phase_index(),
        "total_phases": len(PHASES),
    }


def continue_tutoring(session_id: str, player_message: str) -> dict[str, Any]:
    """Process the player's response and return the next tutor message.

    Handles phase transitions, mastery detection, and session completion.
    """
    state = _sessions.get(session_id)
    if not state:
        return {"error": "session_not_found", "detail": "Tutoring session expired or not found"}

    # Track player message
    state.messages.append({"role": "user", "content": player_message})

    # Check for explicit skip
    if player_message.strip().lower() in {"skip", "let's move on", "next", "move on"}:
        state.skipped = True
        if not state.advance_phase():
            state.mastery_achieved = True
            return _close_session(state)

        journal_context = _build_journal_context(state.star)
        phase_prompt = _build_phase_prompt(state, journal_context)
        state.messages.append({"role": "system", "content": phase_prompt})

    try:
        result = _tutor_request(state.messages)
    except Exception:
        result = {
            "text": "I'm having trouble connecting right now. Let me try again — where were we?",
            "current_phase": state.current_phase,
            "phase_complete": False,
        }

    msg = result.get("text", "")
    phase_complete = result.get("phase_complete", False)
    session_complete = result.get("session_complete", False)

    # Track connections
    for conn_id in result.get("connections_found", []):
        if conn_id not in state.connections_found:
            state.connections_found.append(conn_id)

    if result.get("topic_shifted"):
        state.topic_shifted = result["topic_shifted"]

    # Track player signals
    if state.current_phase == "restate":
        state.player_restated_accurately = phase_complete
    if state.current_phase == "stress_test":
        state.player_found_edge_cases = phase_complete

    # Advance phase if complete
    if phase_complete:
        if not state.advance_phase():
            # All phases done
            state.phases_completed.append(state.current_phase)
            state.mastery_achieved = True
            state.messages.append({"role": "assistant", "content": msg})
            return _close_session(state)
    elif session_complete:
        state.mastery_achieved = True
        state.messages.append({"role": "assistant", "content": msg})
        return _close_session(state)

    # Track assistant message
    state.messages.append({"role": "assistant", "content": msg})

    return {
        "session_id": session_id,
        "message": msg,
        "current_phase": state.current_phase,
        "phase_index": state.phase_index(),
        "total_phases": len(PHASES),
        "phase_complete": phase_complete,
        "mastery_achieved": False,
    }


def end_tutoring(session_id: str) -> dict[str, Any]:
    """End a tutoring session, persist the record, and update star state."""
    state = _sessions.pop(session_id, None)
    if not state:
        return {"error": "session_not_found"}

    # Determine new star stage
    new_stage = _compute_star_stage(state)

    # Persist the tutoring session
    tutoring = TutoringSession.objects.create(
        star=state.star,
        messages=state.messages,
        phases_completed=state.phases_completed,
        mastery_achieved=state.mastery_achieved,
        new_star_stage=new_stage,
        skipped=state.skipped,
        player_restated_accurately=state.player_restated_accurately,
        player_found_edge_cases=state.player_found_edge_cases,
        connections_made=[{"to_star_id": cid, "player_text": ""} for cid in state.connections_found],
        topic_shifted=state.topic_shifted,
    )

    # Update star
    state.star.star_stage = new_stage
    state.star.tutoring_sessions_count += 1
    state.star.last_tutored_at = timezone.now()
    state.star.last_visited_at = timezone.now()
    state.star.save(
        update_fields=[
            "star_stage",
            "tutoring_sessions_count",
            "last_tutored_at",
            "last_visited_at",
        ]
    )

    return {
        "session_id": session_id,
        "tutoring_session_id": str(tutoring.id),
        "phases_completed": state.phases_completed,
        "mastery_achieved": state.mastery_achieved,
        "new_star_stage": new_stage,
    }


def get_tutoring_state(session_id: str) -> dict[str, Any] | None:
    """Return current tutoring state (for polling or recovery)."""
    state = _sessions.get(session_id)
    if not state:
        return None
    return {
        "session_id": session_id,
        "star_id": state.star.id,
        "star_text": state.star.text[:120],
        "current_phase": state.current_phase,
        "phase_index": state.phase_index(),
        "total_phases": len(PHASES),
        "phases_completed": state.phases_completed,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_journal_context(star: Lesson) -> str:
    """Build context from the star's journal entries for richer tutoring."""
    entries = star.journal_entries.order_by("-created_at")[:3]
    if not entries:
        return ""

    lines = ["\nThe player has written about this before:"]
    for i, entry in enumerate(entries, 1):
        excerpt = entry.text[:200]
        if len(entry.text) > 200:
            excerpt += "..."
        lines.append(f"  [{i}] {excerpt}")
    return "\n".join(lines)


def _build_phase_prompt(state: TutoringState, journal_context: str) -> str:
    """Build the tutoring prompt for the current phase."""
    star = state.star
    tags = ", ".join(star.tags[:3]) if star.tags else "general"

    base = f"""Lesson to explore: "{star.text}"

The player learned this from: {star.get_source_type_display()}
Tags: {tags}
This lesson belongs to the cluster: "{star.cluster_label or "ungrouped"}"

Current phase: {state.current_phase}
Phases completed so far: {", ".join(state.phases_completed) if state.phases_completed else "none"}"""

    if journal_context:
        base += f"\n{journal_context}"

    return base


def _compute_star_stage(state: TutoringState) -> str:
    """Compute the new star stage based on engagement depth.

    Proto → Ignited:   first tutoring session
    Ignited → Radiant: 3+ sessions, journal entries, connections
    Radiant → Supernova: many entries, spawned lessons, deep connections
    """
    star = state.star
    sessions = star.tutoring_sessions_count + 1  # including this one
    journal_count = star.journal_entries.count()
    connection_count = star.connections_out.count()

    if sessions >= 8 or journal_count >= 8 or connection_count >= 12:
        return "supernova"
    if sessions >= 3 or journal_count >= 2 or connection_count >= 5:
        return "radiant"
    return "ignited"


def _close_session(state: TutoringState) -> dict[str, Any]:
    """Build the final response for a completed session."""
    return {
        "session_id": state.session_id,
        "message": "",  # No more messages — auto-end
        "current_phase": state.current_phase,
        "phase_index": state.phase_index(),
        "total_phases": len(PHASES),
        "phase_complete": True,
        "mastery_achieved": True,
        "phases_completed": state.phases_completed
        if not state.current_phase
        else state.phases_completed + [state.current_phase],
        "connections_found": state.connections_found,
    }
