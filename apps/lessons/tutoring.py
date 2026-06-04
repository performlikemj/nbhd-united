"""Tutoring service — the user's own assistant exploring their stars with them.

A tutoring session is the player's co-pilot — the same warm, curious assistant
they already talk to — showing up inside their galaxy to walk one star through
5 phases of deeper understanding:

  1. Restate     — can they explain the lesson in their own words?
  2. Deepen      — do they understand WHY it works?
  3. Stress-test — can they find edge cases and exceptions?
  4. Connect     — can they link it to other stars they already have?
  5. Apply       — can they transfer the lesson to a current situation?

The assistant is curious and supportive, not didactic. It learns from how the
player responds — what sticks, where the blind spots are, what they value — and
records *its own explicit judgments* (not backend-computed proxies) into the
``TutoringSession`` model so a future assistant tool can read them back.

Production notes (PR #754):
  * Session state lives in Django cache (Redis in prod, LocMem in tests), NOT a
    module-level dict — Container Apps scales past one replica and an in-process
    dict silently loses sessions on the replica that didn't start them.
  * The cached state is JSON-serializable; the ``Lesson`` is refetched by id.
  * State is tenant-bound; callers that know the tenant verify it before use.
  * LLM spend is attributed to the tenant via ``record_usage`` at the HTTP call
    site. Tests mock ``_tutor_request`` so usage does not fire there (correct).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import Lesson, TutoringSession

logger = logging.getLogger(__name__)

PHASES = ["restate", "deepen", "stress_test", "connect", "apply"]

# Cache key prefix + TTL for active sessions. ~2h covers a long, paused
# exploration without leaking dead sessions forever.
_CACHE_PREFIX = "tutoring:"
_SESSION_TTL_SECONDS = 2 * 60 * 60

# Bound the candidate-neighbor list we hand the model (id-grounding for
# connections) so a hub star with hundreds of edges can't balloon the prompt.
_MAX_CONNECTION_CANDIDATES = 12

_SKIP_PHRASES = {"skip", "let's move on", "let us move on", "next", "move on"}


def _tutoring_model() -> str:
    """Model id for tutoring calls — configurable, sane default. No settings edit."""
    return getattr(settings, "TUTORING_MODEL", "anthropic/claude-sonnet-4.6")


# ---------------------------------------------------------------------------
# System prompt — the player's own assistant, in their galaxy
# ---------------------------------------------------------------------------

TUTOR_SYSTEM = """You are this person's own assistant — the same warm, curious companion they
already talk to every day — and right now you've shown up *inside their galaxy* to explore one
of their stars with them. A star is a lesson they learned from their own life. You're not a
lecturer or an anonymous tutor; you're their co-pilot, genuinely interested in how THEY think,
revisiting something THEY learned, side by side.

**Your persona:**
- Curious, not didactic. Ask questions because you actually want to understand them.
- Warm but not performative. "That's interesting — tell me more" not "AMAZING INSIGHT!!! 🎉"
- Push gently on edge cases; don't interrogate.
- Celebrate real connections and insights — briefly, authentically.
- If they struggle, help them find the answer rather than handing it over.
- Reference their own journal entries and nearby stars (when provided) to make it personal.
- Speak as "we"/"us" where natural — you're exploring their galaxy together.

**The five phases:**
1. **Restate** — Ask them to explain the lesson in their own words. Check they truly understand
   it, not just recall it.
2. **Deepen** — Explore the "why". What's the mechanism? What's the deeper principle?
3. **Stress-test** — Challenge it. When would this NOT apply? What are the edge cases?
4. **Connect** — Help them link it to OTHER stars they already have. Only reference stars from
   the candidate list provided to you (by id) — do not invent star ids.
5. **Apply** — Test transfer. "You're facing [current situation]. How does this lesson adapt?"

**Phase transition rules:**
- Complete each phase before moving on (don't skip ahead).
- Move on when they show genuine understanding AND engagement.
- If they give a one-word answer, probe gently: "Can you say a bit more about that?"
- They CAN skip a phase by saying "let's move on" or similar — respect that.

**End the session:**
- After Apply is complete (or they exit), summarize briefly what you explored together.
- Mention one thing that stood out about how they engaged with this star.
- Don't assign homework or make promises. Just close naturally.

**CRITICAL — Format your response as a single JSON object:**
{"text": "your message", "current_phase": "deepen", "phase_complete": true}

Fields:
- `text` (required): your conversational message (warm, natural).
- `current_phase` (required): one of restate, deepen, stress_test, connect, apply.
- `phase_complete` (required): true when the current phase is done and you're ready to advance.
- `session_complete` (optional): true after Apply is done — the session should end.
- `restated_accurately` (optional bool): your honest judgment of whether they restated the
  lesson accurately in their OWN words (set during/after the restate phase).
- `found_edge_cases` (optional bool): your honest judgment of whether they surfaced real edge
  cases or exceptions (set during/after the stress_test phase).
- `connections_found` (optional): list of star ids the player connected to — ONLY ids from the
  candidate list you were given. If they connected to something NOT in that list, omit the id.
- `connection_text` (optional): free-text description of a connection they made (use this when
  the connection isn't to one of the candidate stars).
- `topic_shifted` (optional): what they drifted toward if it wasn't the star's topic."""


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str:
    key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError("OPENROUTER_API_KEY is not configured")
    return key


def _tutor_request(
    messages: list[dict],
    temperature: float = 0.7,
    *,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Make a tutoring LLM call and return the parsed JSON response.

    Tests patch this and set ``.return_value`` to a parsed dict, so the contract
    is: returns the parsed JSON object. On a successful real HTTP call we also
    write a billing ``UsageRecord`` attributing the spend to ``tenant_id`` (when
    known). Usage recording is non-fatal — a billing hiccup must not break the
    tutoring turn.
    """
    model = _tutoring_model()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {_resolve_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if tenant_id:
        _record_tutoring_usage(tenant_id, model, data.get("usage", {}) or {})

    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def _record_tutoring_usage(tenant_id: str, model: str, usage: dict) -> None:
    """Attribute a tutoring LLM call's spend to the tenant. Never raises."""
    try:
        from apps.billing.services import record_usage
        from apps.tenants.models import Tenant

        tenant = Tenant.objects.filter(id=tenant_id).first()
        if tenant is None:
            return
        record_usage(
            tenant,
            event_type="tutoring",
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model_used=model,
            # Platform-side OpenRouter call (like synthesis/extraction): track the
            # spend for visibility but don't count it against the tenant's monthly
            # cost cap / message quota — playing the game must not lock them out of
            # their assistant.
            is_system=True,
        )
    except Exception:
        logger.exception("tutoring: usage record failed for tenant %s", str(tenant_id)[:8])


# ---------------------------------------------------------------------------
# Session state (cache-backed, JSON-serializable)
# ---------------------------------------------------------------------------


def _new_state(session_id: str, star: Lesson) -> dict[str, Any]:
    """Build a fresh JSON-serializable session-state dict."""
    return {
        "session_id": session_id,
        "star_id": star.id,
        "tenant_id": str(star.tenant_id),
        "messages": [],
        "current_phase": "restate",
        "phases_completed": [],
        # Validated connections: each {"to_star_id": int|None, "player_text": str}.
        "connections_made": [],
        # Honest signals — the model's explicit judgments, captured raw.
        "player_restated_accurately": None,
        "player_found_edge_cases": None,
        "topic_shifted": "",
        "mastery_achieved": False,
        "skipped": False,
    }


def _cache_key(session_id: str) -> str:
    return f"{_CACHE_PREFIX}{session_id}"


def _load_state(session_id: str) -> dict[str, Any] | None:
    """Load session state from cache, or None if expired/missing."""
    return cache.get(_cache_key(session_id))


def _save_state(state: dict[str, Any]) -> None:
    """Persist session state to cache with the session TTL."""
    cache.set(_cache_key(state["session_id"]), state, timeout=_SESSION_TTL_SECONDS)


def _delete_state(session_id: str) -> None:
    cache.delete(_cache_key(session_id))


def _phase_index(current_phase: str) -> int:
    try:
        return PHASES.index(current_phase)
    except ValueError:
        return 0


def _next_phase(current_phase: str) -> str | None:
    idx = _phase_index(current_phase)
    if idx + 1 < len(PHASES):
        return PHASES[idx + 1]
    return None


def _advance_phase(state: dict[str, Any]) -> bool:
    """Move state to the next phase. Returns False if already on the last phase."""
    nxt = _next_phase(state["current_phase"])
    if nxt:
        state["phases_completed"].append(state["current_phase"])
        state["current_phase"] = nxt
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_tutoring(star: Lesson) -> dict[str, Any]:
    """Begin a new tutoring session for a star.

    Returns the first assistant message and session metadata.
    """
    session_id = str(uuid.uuid4())
    state = _new_state(session_id, star)
    tenant_id = state["tenant_id"]

    journal_context = _build_journal_context(star)
    connection_candidates = _build_connection_candidates(star)
    phase_prompt = _build_phase_prompt(state, star, journal_context, connection_candidates)

    state["messages"] = [
        {"role": "system", "content": TUTOR_SYSTEM},
        {"role": "user", "content": phase_prompt},
    ]

    try:
        result = _tutor_request(state["messages"], tenant_id=tenant_id)
    except Exception:
        logger.exception("tutoring: start LLM call failed for star %s", star.id)
        result = {
            "text": (
                f"Let's revisit something you learned: *{star.text[:100]}"
                f"{'...' if len(star.text) > 100 else ''}*\n\n"
                "Can you put it in your own words for me?"
            ),
            "current_phase": "restate",
            "phase_complete": False,
        }

    msg = result.get("text", "")
    current_phase = result.get("current_phase", "restate")
    state["messages"].append({"role": "assistant", "content": msg})
    _save_state(state)

    return {
        "session_id": session_id,
        "message": msg,
        "current_phase": current_phase,
        "phase_index": _phase_index(state["current_phase"]),
        "total_phases": len(PHASES),
    }


def continue_tutoring(session_id: str, player_message: str) -> dict[str, Any]:
    """Process the player's response and return the next assistant message.

    Handles phase transitions, honest-signal capture, connection validation,
    mastery detection, and auto-close.
    """
    state = _load_state(session_id)
    if not state:
        return {"error": "session_not_found", "detail": "Tutoring session expired or not found"}

    star = Lesson.objects.filter(id=state["star_id"]).first()
    if star is None:
        _delete_state(session_id)
        return {"error": "star_not_found", "detail": "The star for this session no longer exists"}

    state["messages"].append({"role": "user", "content": player_message})
    connection_candidates = _build_connection_candidates(star)

    # Explicit skip — advance the phase locally and re-prime the model.
    if player_message.strip().lower() in _SKIP_PHRASES:
        state["skipped"] = True
        if not _advance_phase(state):
            # Skipping past the last phase ends the session.
            state["phases_completed"].append(state["current_phase"])
            state["mastery_achieved"] = True
            _save_state(state)
            return _close_session(state)

        journal_context = _build_journal_context(star)
        phase_prompt = _build_phase_prompt(state, star, journal_context, connection_candidates)
        state["messages"].append({"role": "system", "content": phase_prompt})

    try:
        result = _tutor_request(state["messages"], tenant_id=state.get("tenant_id"))
    except Exception:
        logger.exception("tutoring: continue LLM call failed for session %s", session_id)
        result = {
            "text": "I'm having trouble connecting right now. Let me try again — where were we?",
            "current_phase": state["current_phase"],
            "phase_complete": False,
        }

    msg = result.get("text", "")
    phase_complete = bool(result.get("phase_complete", False))
    session_complete = bool(result.get("session_complete", False))

    # ── Honest signals — capture the model's EXPLICIT judgments, not a proxy.
    # The model may also volunteer them while in a later phase; record whenever
    # present rather than re-deriving them from phase-advancement.
    if "restated_accurately" in result:
        state["player_restated_accurately"] = bool(result["restated_accurately"])
    if "found_edge_cases" in result:
        state["player_found_edge_cases"] = bool(result["found_edge_cases"])

    # ── Connections — validate ids against the real candidate set; never store
    # a hallucinated PK. Unlisted connections fall back to free text.
    _capture_connections(state, result, connection_candidates)

    if result.get("topic_shifted"):
        state["topic_shifted"] = str(result["topic_shifted"])[:100]

    on_last_phase = _next_phase(state["current_phase"]) is None

    # ── Completion: the model says we're done, OR the last phase just completed.
    if session_complete or (phase_complete and on_last_phase):
        if state["current_phase"] not in state["phases_completed"]:
            state["phases_completed"].append(state["current_phase"])
        state["mastery_achieved"] = True
        state["messages"].append({"role": "assistant", "content": msg})
        _save_state(state)
        return _close_session(state)

    # ── Otherwise advance the phase if the model completed it.
    if phase_complete:
        _advance_phase(state)

    state["messages"].append({"role": "assistant", "content": msg})
    _save_state(state)

    return {
        "session_id": session_id,
        "message": msg,
        "current_phase": state["current_phase"],
        "phase_index": _phase_index(state["current_phase"]),
        "total_phases": len(PHASES),
        "phase_complete": phase_complete,
        "mastery_achieved": False,
    }


def end_tutoring(session_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    """End a session, persist a clean assistant-consumable record, update the star.

    ``tenant_id`` (optional): when a caller knows the acting tenant, we verify
    the session belongs to it before mutating anything (tenant isolation across
    a shared cache).
    """
    state = _load_state(session_id)
    if not state:
        return {"error": "session_not_found"}

    if tenant_id is not None and str(state.get("tenant_id")) != str(tenant_id):
        return {"error": "session_not_found"}

    star = Lesson.objects.filter(id=state["star_id"]).first()
    if star is None:
        _delete_state(session_id)
        return {"error": "star_not_found"}

    new_stage = _compute_star_stage_from_star(star)

    tutoring = TutoringSession.objects.create(
        star=star,
        messages=state["messages"],
        phases_completed=state["phases_completed"],
        mastery_achieved=state["mastery_achieved"],
        new_star_stage=new_stage,
        skipped=state["skipped"],
        player_restated_accurately=state["player_restated_accurately"],
        player_found_edge_cases=state["player_found_edge_cases"],
        connections_made=state["connections_made"],
        topic_shifted=state["topic_shifted"],
    )

    star.star_stage = new_stage
    star.tutoring_sessions_count += 1
    star.last_tutored_at = timezone.now()
    star.last_visited_at = timezone.now()
    star.save(
        update_fields=[
            "star_stage",
            "tutoring_sessions_count",
            "last_tutored_at",
            "last_visited_at",
        ]
    )

    _delete_state(session_id)

    return {
        "session_id": session_id,
        "tutoring_session_id": str(tutoring.id),
        "phases_completed": state["phases_completed"],
        "mastery_achieved": state["mastery_achieved"],
        "new_star_stage": new_stage,
    }


def get_tutoring_state(session_id: str) -> dict[str, Any] | None:
    """Return current session state (for polling or recovery), or None."""
    state = _load_state(session_id)
    if not state:
        return None

    star = Lesson.objects.filter(id=state["star_id"]).first()
    star_text = star.text[:120] if star else ""

    return {
        "session_id": session_id,
        "star_id": state["star_id"],
        "star_text": star_text,
        "current_phase": state["current_phase"],
        "phase_index": _phase_index(state["current_phase"]),
        "total_phases": len(PHASES),
        "phases_completed": state["phases_completed"],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_journal_context(star: Lesson) -> str:
    """Build context from the star's journal entries for richer tutoring.

    Parity note: lesson text + journal excerpts already egress to OpenRouter
    today. This keeps that surface unchanged — no USER.md, no people-map.
    """
    entries = star.journal_entries.order_by("-created_at")[:3]
    if not entries:
        return ""

    lines = ["\nThey've written about this before:"]
    for i, entry in enumerate(entries, 1):
        excerpt = entry.text[:200]
        if len(entry.text) > 200:
            excerpt += "..."
        lines.append(f"  [{i}] {excerpt}")
    return "\n".join(lines)


def _build_connection_candidates(star: Lesson) -> list[dict[str, Any]]:
    """Return this star's real neighbor lessons (id + short text) for the same tenant.

    This is the *only* set of star ids the model is allowed to reference in
    ``connections_found`` — it grounds the model so it can't hallucinate PKs.
    Scoped to the star's tenant defensively even though edges are tenant-local.
    """
    neighbors = (
        Lesson.objects.filter(
            connections_in__from_lesson=star,
            tenant_id=star.tenant_id,
            status="approved",
        )
        .exclude(id=star.id)
        .distinct()[:_MAX_CONNECTION_CANDIDATES]
    )
    return [{"id": n.id, "text": n.text[:120]} for n in neighbors]


def _build_phase_prompt(
    state: dict[str, Any],
    star: Lesson,
    journal_context: str,
    connection_candidates: list[dict[str, Any]],
) -> str:
    """Build the tutoring prompt for the current phase."""
    tags = ", ".join(star.tags[:3]) if star.tags else "general"

    base = f"""Star to explore: "{star.text}"

They learned this from: {star.get_source_type_display()}
Tags: {tags}
This star belongs to the cluster: "{star.cluster_label or "ungrouped"}"

Current phase: {state["current_phase"]}
Phases completed so far: {", ".join(state["phases_completed"]) if state["phases_completed"] else "none"}"""

    if journal_context:
        base += f"\n{journal_context}"

    if connection_candidates:
        base += "\n\nNearby stars they already have (reference ONLY these ids in connections_found):"
        for cand in connection_candidates:
            base += f"\n  - id {cand['id']}: {cand['text']}"
    else:
        base += (
            "\n\nThey have no nearby stars connected yet — if they make a connection, capture it "
            "as free text in `connection_text` (do not invent a star id)."
        )

    return base


def _capture_connections(
    state: dict[str, Any],
    result: dict[str, Any],
    connection_candidates: list[dict[str, Any]],
) -> None:
    """Record validated connections into state.

    - ``connections_found`` ids are accepted ONLY if they appear in the
      candidate set (real Lesson PKs for this tenant). Hallucinated ids are
      dropped.
    - ``connection_text`` (free text) is stored with ``to_star_id = None``.
    """
    valid_ids = {cand["id"] for cand in connection_candidates}
    already = {c["to_star_id"] for c in state["connections_made"] if c.get("to_star_id") is not None}

    for raw_id in result.get("connections_found", []) or []:
        try:
            conn_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if conn_id in valid_ids and conn_id not in already:
            state["connections_made"].append({"to_star_id": conn_id, "player_text": ""})
            already.add(conn_id)

    conn_text = result.get("connection_text")
    if conn_text:
        text = str(conn_text)[:500]
        if not any(c.get("to_star_id") is None and c.get("player_text") == text for c in state["connections_made"]):
            state["connections_made"].append({"to_star_id": None, "player_text": text})


def _compute_star_stage_from_star(star: Lesson) -> str:
    """Compute the new star stage based on engagement depth.

    Proto → Ignited:   first tutoring session
    Ignited → Radiant: 3+ sessions, journal entries, or connections
    Radiant → Supernova: many sessions, entries, or deep connections
    """
    sessions = star.tutoring_sessions_count + 1  # including this one
    journal_count = star.journal_entries.count()
    connection_count = star.connections_out.count()

    if sessions >= 8 or journal_count >= 8 or connection_count >= 12:
        return "supernova"
    if sessions >= 3 or journal_count >= 2 or connection_count >= 5:
        return "radiant"
    return "ignited"


def _compute_star_stage(state: Any) -> str:
    """Backwards-compatible stage computation.

    ``test_game.py`` calls this with a ``MagicMock`` carrying ``.star``. Keep the
    name and the ``state.star`` access path so existing unit tests pass.
    """
    return _compute_star_stage_from_star(state.star)


def _close_session(state: dict[str, Any]) -> dict[str, Any]:
    """Build the final response for a completed session.

    ``mastery_achieved`` is True here so the view layer fires ``end_tutoring`` and
    folds the persisted record in under ``session_close``.
    """
    return {
        "session_id": state["session_id"],
        "message": "",  # No more messages — auto-end.
        "current_phase": state["current_phase"],
        "phase_index": _phase_index(state["current_phase"]),
        "total_phases": len(PHASES),
        "phase_complete": True,
        "mastery_achieved": True,
        "phases_completed": list(state["phases_completed"]),
        "connections_made": list(state["connections_made"]),
    }
