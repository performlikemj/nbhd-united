"""Galaxy co-pilot — a spatially-aware in-game line from the user's assistant.

Powers ``POST /api/v1/lessons/galaxy/reflect/``: when the player lands on a star
(or lingers near a neighbourhood), the co-pilot says one warm, grounded line that
connects *this* star to where they've just been in their own knowledge-galaxy,
and optionally points them at a star worth visiting next.

Design (see ``CONTINUITY_galaxy_copilot.md``):
  * **Backend computes the spatial evidence; the LLM only phrases it.** All the
    judgement about *what's near*, *what's stale*, *where to point* is computed
    here in pure Python from the tenant's own data. The model receives that
    evidence and writes a single sentence — it never decides the geometry.
    (``feedback_llm_not_formula_for_judgment``: raw signals from the backend,
    weighed/voiced by the LLM — not a backend-formula verdict.)
  * **PII never egresses raw.** Every free-text field (lesson text, cluster
    labels, notes, the recent path) is redacted before the model call and the
    returned line is rehydrated, so a ``[PERSON_1]`` placeholder can never reach
    the panel. (``project_gravity_pii_egress_gate``,
    ``project_pii_rehydration_egress_gaps``.)
  * **Snappy + cheap + never-empty.** Small fast model, one call, cached by
    (tenant, star, rough ship cell), rate-limited per tenant, usage attributed
    ``is_system`` so playing the game never eats the tenant's quota — and a
    deterministic warm fallback so the panel always has a line, including when
    the shared control-plane OpenRouter key is unavailable
    (``project_control_plane_openrouter_key_stale``).

Coordinate note: ``Lesson.position_x/position_y`` are PCA coords (~[-1, 1]); the
ship's on-screen position is *world* space from the client's layout — a different
basis. So "nearest" here means nearest **in idea-space** (PCA distance to the
target star), which is the semantically meaningful notion anyway: the ideas that
sit closest to this one in the user's mind. The client owns physical, on-screen
proximity and passes it as hints (``nearby_star_ids``) when relevant.
"""

from __future__ import annotations

import logging
import re
from math import hypot
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.pii.redactor import RedactionSession, rehydrate_text

from .models import Lesson, LessonConnection

logger = logging.getLogger(__name__)

# ── Tuning ──────────────────────────────────────────────────────────────────
NEAREST_N = 5  # idea-space neighbours handed to the model
SIMILAR_N = 4  # strongest stored/affinity links of the target
RECENT_N = 5  # how far back the recent flight path is considered
STALE_DAYS = 14  # a star/cluster untouched this long reads as "drifted"
_TEXT_CAP = 160  # per-field text cap before egress (keeps the prompt tight)

# One record-separator the redactor won't treat as PII or rewrite — lets us
# redact every free-text field in a single model pass, then split back.
_SENTINEL = "\n␞\n"

# Cache + rate-limit knobs.
REFLECT_TTL = 180  # re-land at the same star from the same area → same line
_SHIP_CELL = 240.0  # world-space rounding for the cache cell
_RL_PER_MIN = 30  # max LLM-backed reflects per tenant per minute

# Any residual ``[TYPE_N]`` placeholder — scrubbed at the egress boundary so a
# token a rehydration map didn't cover can never reach the panel.
_PLACEHOLDER_RE = re.compile(r"\[[A-Z_]+_\d+\]")


def _copilot_model() -> str:
    """Model id for co-pilot lines — small, fast, warm. Overridable via settings."""
    return getattr(settings, "COPILOT_MODEL", "anthropic/claude-haiku-4.5")


def _copilot_llm_enabled() -> bool:
    """Kill-switch for the live LLM call (env-flippable). Off → deterministic line."""
    return bool(getattr(settings, "COPILOT_LLM_ENABLED", True))


# ── System prompt — the player's own co-pilot, mid-flight ───────────────────
COPILOT_SYSTEM = """You are this person's own assistant — the warm, curious companion they already
talk to — riding along as their co-pilot inside their knowledge-galaxy. Each star is a lesson
they learned from their own life; clusters are constellations of related lessons. They've just
landed somewhere, and you can see exactly where they are and what's around them.

Say ONE short line (1-2 sentences, max ~40 words). Make it land:
- Ground it in the SPECIFIC star they're on and, when it's there, the path they just flew
  (where they came from) or what sits nearest in their thinking.
- Be reflective and warm, never a lecturer. Notice a connection, a return, a drift — don't
  give advice unless they've asked for it.
- Speak as a companion ("you"/"we"), plainly. No emoji, no exclamation-mark hype, no preamble
  like "It looks like". Just the line itself.
- If something nearby has gone quiet or is worth a look, you may gently gesture at it — but at
  most once, and only if the evidence says so.

Return only the line — no JSON, no quotes around it, no labels."""


# ── Spatial-evidence builder (pure: no LLM, no I/O) ─────────────────────────
def _days_since(dt, now) -> int | None:
    if not dt:
        return None
    delta = now - dt
    return max(0, int(delta.total_seconds() // 86400))


def _cap(text: str | None) -> str:
    text = (text or "").strip()
    return text[: _TEXT_CAP - 1] + "…" if len(text) > _TEXT_CAP else text


def build_spatial_context(
    target: Lesson,
    stars: list[Lesson],
    edges_by_star: dict[int, list[tuple[int, float]]],
    recent_ids: list[int],
    *,
    now=None,
) -> dict[str, Any]:
    """Assemble the structured spatial evidence for one reflect call.

    Pure function over already-loaded rows — easy to unit-test and free of LLM
    or coordinate-system surprises. Text fields are RAW here; the caller redacts
    before egress. ``edges_by_star`` maps a star id to ``[(other_id, similarity)]``.
    """
    now = now or timezone.now()
    by_id = {s.id: s for s in stars}
    visited_total = sum(1 for s in stars if s.last_visited_at is not None)
    cluster_ids = {s.cluster_id for s in stars if s.cluster_id is not None}

    # ── Idea-space neighbours: nearest stars to the target in PCA space. The
    #    ship is effectively *at* the target on a land, so this is "what ideas
    #    sit closest to this one in your mind" — coordinate-consistent and more
    #    meaningful than screen pixels.
    tx, ty = target.position_x, target.position_y
    nearest: list[dict[str, Any]] = []
    if tx is not None and ty is not None:
        scored = [
            (hypot(s.position_x - tx, s.position_y - ty), s)
            for s in stars
            if s.id != target.id and s.position_x is not None and s.position_y is not None
        ]
        scored.sort(key=lambda pair: pair[0])
        for dist, s in scored[:NEAREST_N]:
            nearest.append(
                {
                    "id": s.id,
                    "text": _cap(s.text),
                    "cluster_label": s.cluster_label or "",
                    "visited": s.last_visited_at is not None,
                    "dist": round(dist, 4),
                }
            )

    # ── The target's cluster as a *place*: how big, how much you've explored,
    #    whether the whole neighbourhood has gone quiet.
    cluster: dict[str, Any] | None = None
    if target.cluster_id is not None:
        members = [s for s in stars if s.cluster_id == target.cluster_id]
        freshest = None
        for s in members:
            d = _days_since(s.last_visited_at or s.last_tutored_at, now)
            if d is not None and (freshest is None or d < freshest):
                freshest = d
        cluster = {
            "label": target.cluster_label or "",
            "size": len(members),
            "visited": sum(1 for s in members if s.last_visited_at is not None),
            "freshest_days": freshest,
            "stale": (freshest is None or freshest >= STALE_DAYS),
        }

    # ── Strongest semantic links of the target (stored + affinity edges).
    similar: list[dict[str, Any]] = []
    for other_id, sim in sorted(edges_by_star.get(target.id, []), key=lambda p: -p[1])[:SIMILAR_N]:
        other = by_id.get(other_id)
        if other is not None:
            similar.append({"id": other.id, "text": _cap(other.text), "similarity": round(sim, 4)})

    # ── The recent flight path (newest first), resolved to real stars.
    recent_path: list[dict[str, Any]] = []
    seen: set[int] = set()
    for rid in recent_ids[:RECENT_N]:
        if rid == target.id or rid in seen:
            continue
        seen.add(rid)
        s = by_id.get(rid)
        if s is not None:
            recent_path.append({"id": s.id, "text": _cap(s.text)})

    suggestion = _pick_suggestion(target, nearest, stars, now)

    return {
        "target": {
            "id": target.id,
            "text": _cap(target.text),
            "cluster_label": target.cluster_label or "",
            "star_stage": target.star_stage,
            "galaxy_note": _cap(target.galaxy_note),
        },
        "cluster": cluster,
        "nearest": nearest,
        "similar": similar,
        "recent_path": recent_path,
        "totals": {
            "stars": len(stars),
            "visited": visited_total,
            "clusters": len(cluster_ids),
        },
        "suggestion": suggestion,
    }


_STAGE_RANK = {"proto": 0, "ignited": 1, "radiant": 2, "supernova": 3}


def _pick_suggestion(
    target: Lesson,
    nearest: list[dict[str, Any]],
    stars: list[Lesson],
    now,
) -> dict[str, Any] | None:
    """Deterministically choose where to point next (Phase 3 waypoint).

    Backend owns the *which* — the LLM only voices it. Preference order:
      1. The nearest UNVISITED idea-space neighbour (something close you haven't
         opened yet).
      2. A once-bright star (radiant+) you've drifted from (stale or never
         visited) — worth returning to.
      3. None (don't manufacture a nudge when there's nothing real).
    """
    for n in nearest:
        if not n["visited"]:
            return {"id": n["id"], "text": n["text"], "reason": "nearby_unvisited"}

    drifted: list[tuple[int, Lesson]] = []
    for s in stars:
        if s.id == target.id:
            continue
        if _STAGE_RANK.get(s.star_stage, 0) < _STAGE_RANK["radiant"]:
            continue
        d = _days_since(s.last_visited_at, now)
        if d is None or d >= STALE_DAYS:
            drifted.append((d if d is not None else 10_000, s))
    if drifted:
        drifted.sort(key=lambda pair: -pair[0])
        s = drifted[0][1]
        return {"id": s.id, "text": _cap(s.text), "reason": "drifted_bright"}

    return None


# ── PII seam: redact the evidence's free text in one model pass ──────────────
def _redact_context(ctx: dict[str, Any], session: RedactionSession) -> dict[str, Any]:
    """Redact every free-text field in ``ctx`` through one shared session.

    Batched into a single ``redact`` call via a sentinel join so the heavy NER
    model runs once, not per-field. On any split mismatch (defensive), falls
    back to redacting fields individually so a sentinel hiccup can never corrupt
    or leak text. Mutates a copy; the session's ``entity_map`` collects new mints
    for rehydration of the response.
    """
    fields: list[tuple[Any, str]] = []  # (container, key) for each redactable string

    def collect(container: dict[str, Any], key: str) -> None:
        if container.get(key):
            fields.append((container, key))

    out = _deepcopy_ctx(ctx)
    collect(out["target"], "text")
    collect(out["target"], "cluster_label")
    collect(out["target"], "galaxy_note")
    if out["cluster"]:
        collect(out["cluster"], "label")
    for item in out["nearest"]:
        collect(item, "text")
        collect(item, "cluster_label")
    for item in out["similar"]:
        collect(item, "text")
    for item in out["recent_path"]:
        collect(item, "text")
    if out["suggestion"]:
        collect(out["suggestion"], "text")

    if not fields:
        return out

    originals = [container[key] for container, key in fields]
    joined = _SENTINEL.join(originals)
    redacted_join = session.redact(joined)
    parts = redacted_join.split(_SENTINEL)

    if len(parts) == len(originals):
        for (container, key), value in zip(fields, parts):
            container[key] = value
    else:
        # Sentinel got mangled — redact each field on its own (slower, safe).
        for container, key in fields:
            container[key] = session.redact(container[key])

    return out


def _deepcopy_ctx(ctx: dict[str, Any]) -> dict[str, Any]:
    """Shallow-by-structure copy of the context dict (no shared mutable refs)."""
    return {
        "target": dict(ctx["target"]),
        "cluster": dict(ctx["cluster"]) if ctx["cluster"] else None,
        "nearest": [dict(n) for n in ctx["nearest"]],
        "similar": [dict(s) for s in ctx["similar"]],
        "recent_path": [dict(r) for r in ctx["recent_path"]],
        "totals": dict(ctx["totals"]),
        "suggestion": dict(ctx["suggestion"]) if ctx["suggestion"] else None,
    }


# ── Prompt assembly + fallback line (both run on the REDACTED context) ───────
def _evidence_block(ctx: dict[str, Any], mode: str) -> str:
    lines: list[str] = []
    t = ctx["target"]
    verb = "lingering near" if mode == "ambient" else "landed on"
    lines.append(f'They just {verb} this star: "{t["text"]}"')
    if t["star_stage"]:
        lines.append(f"Its stage: {t['star_stage']} (how developed this lesson is).")
    if t["galaxy_note"]:
        lines.append(f'Their pinned note on it: "{t["galaxy_note"]}"')

    c = ctx["cluster"]
    if c and c["label"]:
        explored = f"{c['visited']}/{c['size']} explored"
        quiet = " — this whole neighbourhood has been quiet lately" if c["stale"] else ""
        lines.append(f'Constellation it belongs to: "{c["label"]}" ({explored}){quiet}.')

    if ctx["recent_path"]:
        path = " ← ".join(f'"{r["text"]}"' for r in ctx["recent_path"])
        lines.append(f"The path they flew to get here (most recent first): {path}")

    if ctx["nearest"]:
        near = "; ".join(f'"{n["text"]}"' + ("" if n["visited"] else " (not yet visited)") for n in ctx["nearest"][:3])
        lines.append(f"Nearest ideas in their mind: {near}")

    if ctx["similar"]:
        sim = "; ".join(f'"{s["text"]}"' for s in ctx["similar"][:2])
        lines.append(f"Most strongly linked to: {sim}")

    s = ctx["suggestion"]
    if s:
        why = {
            "nearby_unvisited": "close by and not yet opened",
            "drifted_bright": "a bright star they've drifted away from",
        }.get(s["reason"], "worth a look")
        lines.append(f'If you want to gesture somewhere, this is {why}: "{s["text"]}"')

    return "\n".join(lines)


def _build_messages(ctx: dict[str, Any], mode: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": COPILOT_SYSTEM},
        {"role": "user", "content": _evidence_block(ctx, mode) + "\n\nNow say your one line."},
    ]


def _fallback_line(ctx: dict[str, Any], mode: str) -> str:
    """A warm, grounded line with no model — built from the same redacted evidence.

    Good enough to carry the feature on its own (the LLM is the upgrade, not the
    floor). Kept reflective and specific, never prescriptive.
    """
    t = ctx["target"]
    recent = ctx["recent_path"]
    c = ctx["cluster"]

    if mode == "ambient":
        if c and c["label"]:
            return f"You keep circling {c['label']}. There's a thread here you haven't quite pulled yet."
        return "You've been hovering out here a while — something in this corner is holding you."

    if recent:
        return (
            f'You came here from "{recent[0]["text"]}", and now "{t["text"]}". '
            "Sit with how those two sit next to each other."
        )
    if c and c["label"] and c["stale"]:
        return (
            f'"{t["text"]}" — part of {c["label"]}, a corner you haven\'t flown through in a while. Welcome back to it.'
        )
    if ctx["nearest"]:
        return (
            f"\"{t['text']}\". It doesn't stand alone — there's a cluster of nearby thinking around it worth tracing."
        )
    return f'"{t["text"]}". Before anything else: how would you put this in your own words, right now?'


# ── LLM call (mirrors tutoring._tutor_request) ──────────────────────────────
def _resolve_api_key() -> str:
    key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError("OPENROUTER_API_KEY is not configured")
    return key


def _copilot_request(messages: list[dict], *, tenant_id: str | None = None) -> str:
    """One co-pilot LLM call → the plain-text line. Records system-attributed usage.

    Raises on any HTTP / parse failure so the caller can fall back. Tests patch
    this directly (set ``.return_value`` to a string).
    """
    model = _copilot_model()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {_resolve_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 120,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    if tenant_id:
        _record_copilot_usage(tenant_id, model, data.get("usage", {}) or {})

    return (data["choices"][0]["message"]["content"] or "").strip()


def _record_copilot_usage(tenant_id: str, model: str, usage: dict) -> None:
    """Attribute a co-pilot call's spend to the tenant (system-side). Never raises."""
    try:
        from apps.billing.services import record_usage
        from apps.tenants.models import Tenant

        tenant = Tenant.objects.filter(id=tenant_id).first()
        if tenant is None:
            return
        record_usage(
            tenant,
            event_type="copilot",
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model_used=model,
            # Platform-side OpenRouter call (like tutoring/synthesis): tracked for
            # visibility but never counted against the tenant's quota — exploring
            # the galaxy must not lock them out of their assistant.
            is_system=True,
        )
    except Exception:
        logger.exception("copilot: usage record failed for tenant %s", str(tenant_id)[:8])


def _clean_line(line: str) -> str:
    """Strip stray wrapping quotes / labels a model sometimes adds."""
    line = (line or "").strip()
    if len(line) >= 2 and line[0] in "\"'“‘" and line[-1] in "\"'”’":
        line = line[1:-1].strip()
    return line


# ── Orchestrator ────────────────────────────────────────────────────────────
def reflect(
    *,
    tenant,
    target: Lesson,
    stars: list[Lesson],
    edges_by_star: dict[int, list[tuple[int, float]]],
    recent_ids: list[int],
    mode: str = "land",
    allow_llm: bool = True,
) -> dict[str, Any]:
    """Build evidence → redact → phrase (LLM or fallback). Returns the line still
    REDACTED — rehydration happens at the egress boundary (``finalize_egress``).

    Returns ``{"line", "point": {star_id, label, reason} | None, "source", "_mints"}``
    where ``line``/``point.label`` carry ``[TYPE_N]`` placeholders and ``_mints`` is
    this call's freshly-minted entity map. Deferring rehydration to egress means a
    cached redacted line is never served with a stale name, and the single scrub
    point guarantees no placeholder leaks. Never raises for an LLM failure —
    degrades to the deterministic line. ``allow_llm=False`` skips the network call.
    """
    ctx = build_spatial_context(target, stars, edges_by_star, recent_ids)

    session = RedactionSession(tenant=tenant)
    redacted = _redact_context(ctx, session)

    source = "fallback"
    line = _fallback_line(redacted, mode)
    if allow_llm and _copilot_llm_enabled():
        try:
            raw = _copilot_request(_build_messages(redacted, mode), tenant_id=str(tenant.id))
            cleaned = _clean_line(raw)
            if cleaned:
                line = cleaned
                source = "llm"
        except Exception:
            logger.info("copilot: LLM call failed for star %s — using fallback", target.id, exc_info=True)

    point = None
    sug = redacted["suggestion"]
    if sug:
        point = {"star_id": sug["id"], "label": sug["text"], "reason": sug["reason"]}

    return {"line": line, "point": point, "source": source, "_mints": dict(session.entity_map)}


def _scrub_placeholders(text: str) -> str:
    """Belt-and-suspenders: drop any residual ``[TYPE_N]`` token a rehydration map
    didn't cover (e.g. one the model hallucinated). The PII contract is that no
    such token reaches the user — replace it with a neutral word, never leak it.
    """
    if not text or "[" not in text:
        return text
    leftovers = _PLACEHOLDER_RE.findall(text)
    if not leftovers:
        return text
    logger.warning("copilot: scrubbed %d residual PII placeholder(s) from an egress line", len(leftovers))
    cleaned = _PLACEHOLDER_RE.sub("someone", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def finalize_egress(tenant, result: dict[str, Any]) -> dict[str, Any]:
    """Rehydrate the redacted line/label and scrub residuals — the single egress seam.

    Applied to EVERY response (cache hit or miss): rehydration uses the LIVE tenant
    entity map unioned with this call's own fresh mints (``_mints``), so a cached
    redacted line is never served with a stale name, and a hallucinated placeholder
    is scrubbed rather than leaked. ``_mints`` is dropped from the client payload.
    """
    mints = result.pop("_mints", None) or {}
    rehydrate_map = {**(getattr(tenant, "pii_entity_map", None) or {}), **mints}

    line = result.get("line") or ""
    if rehydrate_map:
        line = rehydrate_text(line, rehydrate_map)
    result["line"] = _scrub_placeholders(line)

    point = result.get("point")
    if point and point.get("label"):
        label = rehydrate_text(point["label"], rehydrate_map) if rehydrate_map else point["label"]
        point["label"] = _scrub_placeholders(label)

    return result


# ── View-layer helpers: edges, cache, rate limit ────────────────────────────
def load_edges_for(target_id: int, star_ids: set[int]) -> dict[int, list[tuple[int, float]]]:
    """Stored LessonConnections touching the target, scoped to the visible stars.

    Cheap (indexed FK lookups) — we deliberately skip the O(n^2) embedding
    affinity pass the full galaxy endpoint does, so a reflect stays snappy.
    """
    edges: dict[int, list[tuple[int, float]]] = {}
    conns = LessonConnection.objects.filter(from_lesson_id=target_id).values_list("to_lesson_id", "similarity")
    for other_id, sim in conns:
        if other_id in star_ids:
            edges.setdefault(target_id, []).append((other_id, float(sim or 0.0)))
    # Connections are bidirectional in practice, but include the reverse just in case.
    rev = LessonConnection.objects.filter(to_lesson_id=target_id).values_list("from_lesson_id", "similarity")
    have = {oid for oid, _ in edges.get(target_id, [])}
    for other_id, sim in rev:
        if other_id in star_ids and other_id not in have:
            edges.setdefault(target_id, []).append((other_id, float(sim or 0.0)))
    return edges


def reflect_cache_key(tenant_id, mode: str, star_id: int, ship: dict | None) -> str:
    cx = cy = 0
    if ship:
        try:
            cx = int(round(float(ship.get("x", 0)) / _SHIP_CELL))
            cy = int(round(float(ship.get("y", 0)) / _SHIP_CELL))
        except (TypeError, ValueError):
            cx = cy = 0
    return f"copilot:reflect:{tenant_id}:{mode}:{star_id}:{cx}:{cy}"


def allow_llm_for(tenant_id) -> bool:
    """Fixed-window per-tenant rate limit on LLM-backed reflects.

    Returns True if this call may hit the model. Over the limit, callers still
    return a (fallback) line — the panel is never empty, we just stop spending.
    Best-effort: a cache hiccup fails open (allow), since the line itself is cheap.
    """
    minute = int(timezone.now().timestamp() // 60)
    key = f"copilot:rl:{tenant_id}:{minute}"
    try:
        added = cache.add(key, 0, timeout=120)  # seed the window once
        count = cache.incr(key)
        if added:
            pass
        return count <= _RL_PER_MIN
    except Exception:
        return True
