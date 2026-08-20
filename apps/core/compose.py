"""Core meditation *authoring* — turn raw signals into a render manifest via an LLM.

The locked split is "assistant authors the manifest (judgment); backend renders
(deterministic)". This is the backend-invoked authoring path used by the web orb
(no OpenClaw plugin required): gather raw signals → ask an LLM to author the sit
within the phase-arc grammar (fixed bookends, its choice of middle — see
``render.MIDDLE_PHASES``) → validate before any TTS spend.

LLM access mirrors the project's other Django-side calls (apps/insights/synthesis.py,
apps/pii/arbiter.py): the shared ``apps.common.openrouter.chat_completion`` client
with JSON mode. Like those callers it tries an ORDERED CHAIN of low-cost OpenRouter
models rather than a single one, so a single model's hiccup on this structured task
no longer fails the whole compose. Each candidate is parsed AND validated — a model
that answers with unusable *content* (not just an empty body) also falls through to
the next. Kept LEAN on purpose — sparse speech (~6-12 TTS calls) so a low Gemini
tier's per-minute cap isn't tripped; silence (free) does the rest of the 10 minutes.
"""

from __future__ import annotations

import json
import logging
import time

from django.conf import settings

from apps.billing.constants import DEEPSEEK_FLASH_MODEL, DEEPSEEK_MODEL, GEMMA_MODEL
from apps.common.openrouter import chat_completion
from apps.core import render

logger = logging.getLogger(__name__)

# Authoring chain (primary overridable via settings.CORE_COMPOSE_MODEL), de-duped,
# primary first — mirrors synthesis.SYNTHESIS_MODELS / arbiter.ARBITER_MODELS so a
# single-model hiccup never sinks the compose. All low-cost OpenRouter models.
# STEERABLE first, not merely cheapest: Gemma 4 31B (Gemini family — on OpenRouter's
# structured-output list, English-native, NOT a reasoning model) leads, because this
# is a structured-authoring task. The DeepSeek V4 tiers are reasoning models that
# OpenRouter does NOT list for structured outputs — under loose JSON mode they burn
# output tokens on a reasoning trace (→ truncated JSON) and drift in language /
# segment count — so they sit BEHIND Gemma as cheap fallbacks (Flash before the
# pricier Pro), reached only if Gemma is unavailable or returns unusable content.
DEFAULT_COMPOSE_MODEL = GEMMA_MODEL
_FALLBACK_COMPOSE_MODELS = [DEEPSEEK_FLASH_MODEL, DEEPSEEK_MODEL]
_MAX_OUTPUT_TOKENS = 3000  # headroom: holistic manifests carry many explicit-silence segments
_LLM_TIMEOUT_S = 60
# A transient hiccup (timeout / 429 / 5xx) on the FIRST attempt of a model gets
# ONE retry after a short backoff before we give up on that model and fall to the
# next. Content failures (unparseable / invalid manifest) are NOT retried — a
# retry can't fix bad content, so we fall straight through to the next model.
_RETRY_BACKOFF_S = 2.0
_TRANSIENT_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "timeout",
    "timed out",
    "connection",
    "temporarily",
    "rate limit",
)

# The look-back block is the one prompt section that grows with use — a daily
# sitter accumulates history forever. Cap it so the recent list can never crowd
# out this week's actual signals (whole entries only; oldest dropped first).
_RECENT_MEDITATIONS_CHAR_BUDGET = 1200

# Rough per-phase time budgets (seconds) for the ~10-min arc — guidance the model
# paces toward and a fallback when it omits one. The model now OWNS the allocation
# (holistic pacing); these are no longer force-applied. Every name in the phase
# grammar has an entry: ``_normalize`` already falls back to 60s for an unknown
# name, but 60s is plain wrong for a long ``open_sit``, and a phase target is what
# a ``flex`` silence expands to fill — so the deep phases get honest defaults.
_PHASE_TARGETS = {
    "arrival": 60,
    "breath_anchor": 75,
    "body_scan": 150,
    "core_practice": 210,
    "integration": 60,
    "closing": 45,
    "settle": 90,
    "open_sit": 210,
    "re_anchor": 45,
    "teaching": 90,
}


class ComposeError(RuntimeError):
    """Authoring failed (LLM unavailable / unparseable / invalid manifest)."""


_SYSTEM_PROMPT = (
    "You are a meditation guide composing a personalized ~10-minute guided meditation for ONE person, "
    "drawn from what their assistant has learned about their week. You output ONLY a JSON render manifest "
    "that a deterministic backend voices with TTS and stitches together with programmatic silence.\n\n"
    "Compose it HOLISTICALLY. In a real guided meditation words are sparse and silence does the work: you "
    "speak to guide attention, then leave long, unhurried space for the person to actually practice. YOU "
    "decide where words are needed, where silence is needed, and how long each silence holds. Never force "
    "words in where stillness would serve better.\n\n"
    # The signals below are read straight from placeholder-space storage (journal,
    # daily notes, constellation, goals, the last sit's theme) and are NOT rehydrated
    # before they reach this prompt, so redaction tokens routinely appear in the input.
    # The entity legend only shows up when the tenant has annotated entries, so this
    # note is the permanent explanation.
    "PRIVACY PLACEHOLDERS IN YOUR INPUT:\n"
    "The signals you are given are privacy-scrubbed. A token like [PERSON_1], [LOCATION_2] or "
    "[EMAIL_ADDRESS_3] — sometimes annotated, e.g. [PERSON_1|a coworker] — is a REDACTION PLACEHOLDER "
    "standing in for one piece of this person's private data. Treat each as an opaque proper noun: the "
    "same token always means the same thing, different tokens mean different things. Never guess, invent, "
    "expand, translate, or reason about the identity behind one. If a token must survive into your output, "
    "copy it through character-for-character — but for this task none should: narration is second-person, "
    'so write "you" rather than naming anyone. A token left in spoken text gets read aloud as brackets.\n\n'
    "Output a JSON object with EXACTLY these keys:\n"
    "  schema_version: 1\n"
    "  title: a short, evocative title (<= 60 chars)\n"
    "  theme: one sentence — the personalized through-line for this sit\n"
    '  voice: "Achernar"\n'
    '  global_tone: "soft, slow, warm; unhurried with generous space"\n'
    "  total_target_seconds: 600\n"
    "  ambient: null\n"
    "  phases: the sit's arc — you choose today's shape, within the grammar below\n\n"
    "PHASE ARC — the bookends are fixed, the middle is yours:\n"
    "- The FIRST phase is always arrival and the LAST is always closing.\n"
    "- Between them put 1 to 5 phases, each named from exactly this list: breath_anchor, body_scan, "
    "core_practice, integration, settle, open_sit, re_anchor, teaching.\n"
    "- A name may appear more than once, but never twice in a row (that is one longer phase, not two).\n"
    "- Choose the arc that serves this person today, and vary it against the recent sits. Three that work:\n"
    "    classic     — arrival, breath_anchor, body_scan, core_practice, integration, closing\n"
    "    spacious    — arrival, settle, open_sit, re_anchor, open_sit, closing\n"
    "    lesson-led  — arrival, breath_anchor, teaching, core_practice, integration, closing\n\n"
    "Each phase is an object: { name, intent (short), target_seconds, segments[] }, where target_seconds is "
    "your rough time budget for that phase (the phases together should sum to ~600). Put segments in the order "
    "they play. A segment is either:\n"
    '  { "type": "speech", "text": "...", "tone": "..." }   — a short spoken guidance, 1-3 sentences\n'
    '  { "type": "silence", "seconds": <integer 3-150> }    — a held pause of exactly that length\n'
    '  { "type": "silence", "seconds": "flex" }             — OPTIONAL: a pause the backend expands to fill the phase budget\n\n'
    "PACING — this is the craft:\n"
    "- Speak briefly, then hold. The natural rhythm is a short guidance followed by a long silence.\n"
    "- Use GENEROUS silences. 60, 90, even 120 seconds of stillness is normal and welcome — most of all in the "
    "deep middle of the sit (body_scan, core_practice, an open_sit), where the person is doing the inner work. "
    "The opening phases may be a little more spoken; the deep middle should be mostly silent.\n"
    "- A phase can be almost all silence with a single spoken cue — or, occasionally, need no new words at all.\n"
    "- Across the WHOLE sit keep it to roughly 3-10 spoken moments (never fewer than 3, never more than 12). "
    "Sparse on purpose.\n"
    '- Set real silence lengths yourself; reach for a single "flex" in a phase only when you want the backend '
    "to fill that phase's remaining time for you.\n\n"
    # Density, variety, the wisdom lesson, and the takeaway (meditation v2 phase 1).
    # The person sits most days; without these the model re-composed the same title
    # and the same through-line indefinitely, because the only prior data point it
    # ever saw was the last theme. The RECENT MEDITATIONS block referenced below is
    # emitted by _format_signals whenever gather_meditation_signals found prior sits.
    "DENSITY — vary how much you speak from day to day:\n"
    "- Some sits are SPACIOUS: very few words, long silences, and only a brief re-anchoring cue when "
    "attention would have wandered. Others are more GUIDED: shorter silences, more frequent cues.\n"
    "- Pick today's density deliberately and vary it against the recent sits — a person who sits daily "
    "should not get the same amount of talking every time.\n"
    "- Express the choice ONLY through how you compose segments: how many spoken moments, how long the "
    "silences hold, and which arc you pick. A spacious arc may go as low as 3 spoken moments — settling in, "
    "one re-anchoring cue, the closing takeaway — while a guided one sits at the high end. Never name the "
    "density and never add a key for it.\n\n"
    "VARIETY — every sit must feel like a new doorway:\n"
    "- Your input may include a RECENT MEDITATIONS list (newest first, with each sit's title and theme). "
    "That list is what you must NOT repeat.\n"
    "- The title must not repeat or near-echo any recent title — not the same words, and not the same "
    "phrase with one word swapped. If the title you want resembles one on that list, choose a different one.\n"
    "- Vary the imagery you reach for, how you open the sit, and what the practice focuses on, measured "
    "against those recent sits.\n\n"
    "WISDOM LESSON — each sit teaches ONE small idea:\n"
    "- Draw it from the breadth of contemplative thought: Stoic, Zen, broader Buddhist, Taoist, Sufi, "
    "Christian-contemplative, Jewish, or secular philosophy and the science of mind.\n"
    "- CHOOSE the idea to serve what today's signals show this person is carrying — not at random. Rotate "
    "which tradition you draw on against the recent sits.\n"
    "- Teach it plainly and invitationally, in a sentence or two, within core_practice or a teaching "
    'phase. Attribute it simply where that helps ("the Stoics called this…", "there is a Zen word for this…").\n'
    "- Never preach, never argue for a tradition, and never presume what this person believes or practices. "
    "You are offering one idea to try on for a few minutes, not a doctrine.\n\n"
    "TAKEAWAY:\n"
    "- The LAST speech segment of the closing phase is one carryable sentence that distills the lesson into "
    "something they can hold for the rest of the day. Plain words; not a summary of the sit.\n\n"
    "HARD RULES (the manifest is rejected otherwise):\n"
    "- The closing phase must END on a speech segment, so the sit lands rather than trailing into silence.\n"
    "- Speech text is short (1-3 sentences), calm, second-person, present-tense. Never read instructions aloud.\n"
    '- Always address them as "you". NEVER use a name or any [BRACKETED_TOKEN] — those are voiced literally by TTS.\n'
    "- The heart of the sit — core_practice, or whichever middle phase carries it — is the personalized part: "
    "gently name what the signals suggest they're carrying, and offer permission to set it down. Be specific "
    "but kind; never clinical, never list their data back.\n"
    "- closing gives one small carry-forward intention.\n\n"
    "OUTPUT FORMAT (strict):\n"
    "- Write ALL text — title, theme, every speech segment — in ENGLISH.\n"
    "- Return ONLY the JSON object: a single object (not an array), with no prose, no markdown fences, and no "
    "reasoning or commentary before or after it. Keep it compact so it is never truncated."
)


def _target_seconds_from_signals(signals: dict) -> float:
    """The sit's target length (s) from ``preferred_duration_minutes``.

    Clamped to the band the renderer supports (matches ``CoreProfile``'s 3–30 min);
    defaults to the canonical ~10-minute sit when the signal is absent/invalid.
    """
    minutes = signals.get("preferred_duration_minutes")
    try:
        seconds = float(int(minutes) * 60) if minutes else render.DEFAULT_TOTAL_TARGET_SECONDS
    except (TypeError, ValueError):
        seconds = render.DEFAULT_TOTAL_TARGET_SECONDS
    return min(render.HARD_MAX_TOTAL_SECONDS, max(180.0, seconds))


def _constellation_line(star: dict) -> str:
    """One gentle, meditation-tone line for an actively-worked constellation star.

    Selection + phrasing only (no scoring). The raw note/reflection text is passed
    through for the guide to weave — never to read back verbatim; the system prompt
    forbids voicing names or listing the person's data aloud.
    """
    text = " ".join(str(star.get("text", "")).split())[:160]
    insights = star.get("tutoring_insights") or []
    latest = insights[0] if insights else {}
    stage = star.get("stage", "")

    if latest.get("mastery_achieved"):
        engagement = "something they've been settling into"
    elif latest.get("restated_accurately") is False:
        engagement = "something still taking shape for them"
    elif latest.get("found_edge_cases"):
        engagement = "a place they've been probing the edges"
    elif stage in ("radiant", "supernova"):
        engagement = "a steady, well-worn insight of theirs"
    else:
        engagement = "something they've been revisiting"

    parts = [f'- "{text}" — {engagement}.']
    note = " ".join(str(star.get("galaxy_note", "")).split())
    if note:
        parts.append(f' They pinned a note on it: "{note[:200]}".')
    entries = star.get("journal_entries") or []
    reflection = " ".join(str(entries[0].get("text", "")).split()) if entries else ""
    if reflection:
        parts.append(f' Recent reflection: "{reflection[:200]}".')
    return "".join(parts)


def _recent_meditation_lines(entries: list, *, budget: int = _RECENT_MEDITATIONS_CHAR_BUDGET) -> list[str]:
    """Newest-first look-back lines, trimmed to a fixed character budget.

    Whole entries only: once the budget is spent the remaining (oldest) entries are
    dropped rather than cut mid-line, so the guide never reads half a title and
    treats it as a real one. The cap is what keeps a daily sitter's history from
    slowly crowding out the rest of the prompt.
    """
    lines: list[str] = []
    used = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        date = str(entry.get("date") or "").strip()
        title = " ".join(str(entry.get("title") or "").split())
        theme = " ".join(str(entry.get("theme") or "").split())
        if not title and not theme:
            continue
        line = f"- {date}: " if date else "- "
        line += f'"{title}"' if title else "(untitled)"
        if theme:
            line += f" — {theme}"
        if used + len(line) + 1 > budget:  # +1 for the newline it will be joined with
            break
        lines.append(line)
        used += len(line) + 1
    return lines


def _format_signals(signals: dict, target_seconds: float = render.DEFAULT_TOTAL_TARGET_SECONDS) -> str:
    """Render the gathered signals as a compact prompt context (no raw PII dumps)."""
    lines: list[str] = ["Here is what you've gathered about this person's recent days:"]
    recent_meditations = _recent_meditation_lines(signals.get("recent_meditations") or [])
    if recent_meditations:
        lines.append("RECENT MEDITATIONS (newest first):")
        lines.extend(recent_meditations)
    notes = signals.get("recent_notes") or []
    if notes:
        lines.append("Recent daily-note snippets:")
        lines.extend(f"- {n}" for n in notes[:6])
    stars = signals.get("constellation_stars") or []
    if stars:
        lines.append(
            "Stars they've been working through in their constellation (durable lessons they're "
            "actively revisiting) — let these gently shape the heart of the sit:"
        )
        lines.extend(_constellation_line(s) for s in stars[:4])
    goals = signals.get("active_goals") or []
    if goals:
        lines.append(
            "What they're actively working toward (their stated goals — hold these gently, don't recite them):"
        )
        lines.extend(f"- {g}" for g in goals[:5])
    if signals.get("fuel_summary"):
        lines.append(f"Recent movement: {signals['fuel_summary']}")
    if signals.get("additional_context"):
        lines.append(f"What they've asked to keep in mind: {signals['additional_context']}")
    if len(lines) == 1:
        lines.append(
            "- (little specific signal this week — compose a gentle, universal sit about arriving, "
            "breathing, and setting down whatever today held)"
        )
    minutes = max(1, round(target_seconds / 60))
    scale = target_seconds / render.DEFAULT_TOTAL_TARGET_SECONDS
    # The pacing reference is shown for the CLASSIC arc only — it is an example of
    # how much time a phase wants, not the arc to compose. Whichever arc is chosen,
    # what's binding is that its phases sum to the target.
    scaled = ", ".join(f"{name}~{max(1, round(_PHASE_TARGETS[name] * scale))}s" for name in render.CLASSIC_ARC)
    lines.append(
        f"\nCompose today's sit now — holistically. Aim for about {minutes} minutes "
        f"(~{round(target_seconds)}s total); let silence carry most of it; speak only where it guides. "
        f"Whichever arc you choose, its phases should sum to ~{round(target_seconds)}s — for a sense of scale, "
        f"the classic arc paces roughly: " + scaled + "."
    )
    return "\n".join(lines)


def _normalize(manifest: dict, voice: str, target_seconds: float = render.DEFAULT_TOTAL_TARGET_SECONDS) -> dict:
    """Backstop the scaffold fields the model must not drift on, before validation.

    The per-phase time budgets are the model's to allocate (holistic pacing) — we
    only fill a sane default when it omits one, so the renderer always has a
    positive ``target_seconds`` for any flex silence to resolve against. We DO own
    the overall budget: ``total_target_seconds`` is pinned to the user's chosen
    length (it's the contract the render-time ceiling validates against), and any
    omitted per-phase target defaults are scaled to it.
    """
    if not isinstance(manifest, dict):
        raise ComposeError("LLM did not return a JSON object")
    scale = target_seconds / render.DEFAULT_TOTAL_TARGET_SECONDS
    manifest.setdefault("schema_version", 1)
    manifest["voice"] = voice or manifest.get("voice") or render.DEFAULT_VOICE
    manifest.setdefault("global_tone", "soft, slow, warm; unhurried with generous space")
    manifest["total_target_seconds"] = int(round(target_seconds))
    manifest.setdefault("ambient", None)
    for phase in manifest.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        try:
            if float(phase.get("target_seconds")) > 0:
                continue  # keep the model's allocation
        except (TypeError, ValueError):
            pass
        phase["target_seconds"] = max(1, round(_PHASE_TARGETS.get(phase.get("name"), 60) * scale))
    return manifest


def _compose_models(model: str = "") -> list[str]:
    """The ordered authoring chain: explicit override (alone) OR primary + fallbacks.

    An explicit ``model`` arg pins a single model (callers/tests that want one). With
    no override, the chain is the configured primary first, then the fallbacks,
    de-duped and preserving order.
    """
    if model:
        return [model]
    primary = getattr(settings, "CORE_COMPOSE_MODEL", "") or DEFAULT_COMPOSE_MODEL
    chain = [primary, *_FALLBACK_COMPOSE_MODELS]
    seen: set[str] = set()
    return [m for m in chain if m and not (m in seen or seen.add(m))]


def _strip_code_fences(text: str) -> str:
    """Drop a leading/trailing ```json fence if the model wrapped its JSON in one.

    Mirrors apps.pii.arbiter — some models emit fenced JSON even under
    ``response_format=json_object``.
    """
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


def _is_transient(exc: Exception) -> bool:
    """A transport hiccup worth one retry (timeout / rate-limit / 5xx), vs. a hard
    failure (bad content, auth) where a retry can't help."""
    return any(marker in str(exc).lower() for marker in _TRANSIENT_MARKERS)


def _call_model(model_id: str, messages: list, api_key: str, body: dict) -> tuple[dict, str]:
    """One model's chat_completion with a single transient-error retry + backoff.

    Content problems fall through to the caller unretried (the next model handles
    them); only a transient transport error triggers the one backed-off retry.
    """
    try:
        return chat_completion(model_id, messages, api_key=api_key, timeout=_LLM_TIMEOUT_S, **body)
    except Exception as exc:  # noqa: BLE001 — decide retry vs. propagate
        if not _is_transient(exc):
            raise
        logger.info("compose: transient error on %s (%s) — one retry after backoff", model_id, str(exc)[:120])
        time.sleep(_RETRY_BACKOFF_S)
        return chat_completion(model_id, messages, api_key=api_key, timeout=_LLM_TIMEOUT_S, **body)


def author_manifest(signals: dict, *, voice: str = "", model: str = "", tenant=None) -> dict:
    """Author a validated render manifest from raw signals via the LLM chain.

    Tries each model in ``_compose_models`` in order; the first whose response
    parses, normalizes, AND passes ``render.validate_manifest`` wins. Each model
    gets ONE backed-off retry on a transient transport error (timeout / 429 / 5xx)
    before it's abandoned. A candidate that fails transport (after its retry),
    returns unparseable JSON, returns valid JSON that isn't a manifest object, or
    returns a structurally-invalid manifest is logged and the next candidate is
    tried. Raises ``ComposeError`` if the key is missing or every candidate fails
    (carrying every candidate's failure reason for diagnosis).
    """
    api_key = getattr(settings, "OPENROUTER_API_KEY", "") or ""
    if not api_key:
        raise ComposeError("OPENROUTER_API_KEY not configured")
    target_seconds = _target_seconds_from_signals(signals)
    from apps.pii.egress import append_entity_legend, redact_known_values

    guarded_signals = redact_known_values(
        tenant,
        _format_signals(signals, target_seconds),
        seam="meditation_compose_prompt",
    )
    guarded_signals = append_entity_legend(
        tenant,
        guarded_signals,
        seam="meditation_compose_prompt",
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": guarded_signals},
    ]
    body = {
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }

    candidates = _compose_models(model)
    failures: list[str] = []

    def _record(model_id: str, reason: str) -> None:
        failures.append(f"{model_id}: {reason}")
        logger.warning("compose: model %s failed — %s", model_id, reason)

    for model_id in candidates:
        try:
            data, _used = _call_model(model_id, messages, api_key, body)
            content = _strip_code_fences((data["choices"][0]["message"]["content"] or "").strip())
        except Exception as exc:  # noqa: BLE001 — record + fall through to the next model
            _record(model_id, f"LLM call failed: {str(exc)[:160]}")
            continue

        try:
            manifest = json.loads(content)
        except json.JSONDecodeError:
            _record(model_id, f"non-JSON response: {content[:120]!r}")
            continue

        # _normalize raises ComposeError on valid-but-non-dict JSON (e.g. a top-level
        # array or quoted string — a real drift mode); treat that like any other bad
        # content and fall through rather than aborting the whole chain.
        try:
            manifest = _normalize(manifest, voice, target_seconds)
        except ComposeError as exc:
            _record(model_id, str(exc))
            continue

        errors = render.validate_manifest(manifest)
        if errors:
            _record(model_id, "invalid manifest: " + "; ".join(errors[:3]))
            continue

        return manifest

    detail = " | ".join(failures) if failures else "no compose model configured"
    raise ComposeError(detail)
