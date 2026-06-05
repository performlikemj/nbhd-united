"""Core meditation *authoring* — turn raw signals into a render manifest via an LLM.

The locked split is "assistant authors the manifest (judgment); backend renders
(deterministic)". This is the backend-invoked authoring path used by the web orb
(no OpenClaw plugin required): gather raw signals → ask an LLM to fill the FIXED
6-phase scaffold (it never invents the structure) → validate before any TTS spend.

LLM access mirrors the project's other Django-side calls (apps/insights/synthesis.py,
apps/pii/arbiter.py): a direct OpenRouter ``chat/completions`` POST with JSON mode.
Kept LEAN on purpose — one speech line per phase (~6 TTS calls) so a low Gemini
tier's per-minute cap isn't tripped; silence (free) does the rest of the 10 minutes.
"""

from __future__ import annotations

import json
import logging

import requests
from django.conf import settings

from apps.core import render

logger = logging.getLogger(__name__)

# Reuse the project's proven Django-side LLM (OpenRouter, JSON mode). Swappable.
DEFAULT_COMPOSE_MODEL = "deepseek/deepseek-v4-pro"
_MAX_OUTPUT_TOKENS = 2000  # holistic manifests carry more segments (explicit silences)
_LLM_TIMEOUT_S = 60

# Rough per-phase time budgets (seconds) for the ~10-min arc — guidance the model
# paces toward and a fallback when it omits one. The model now OWNS the allocation
# (holistic pacing); these are no longer force-applied.
_PHASE_TARGETS = {
    "arrival": 60,
    "breath_anchor": 75,
    "body_scan": 150,
    "core_practice": 210,
    "integration": 60,
    "closing": 45,
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
    "Output a JSON object with EXACTLY these keys:\n"
    "  schema_version: 1\n"
    "  title: a short, evocative title (<= 60 chars)\n"
    "  theme: one sentence — the personalized through-line for this sit\n"
    '  voice: "Achernar"\n'
    '  global_tone: "soft, slow, warm; unhurried with generous space"\n'
    "  total_target_seconds: 600\n"
    "  ambient: null\n"
    "  phases: an array of EXACTLY these 6 phases, IN THIS ORDER:\n"
    "    arrival, breath_anchor, body_scan, core_practice, integration, closing\n\n"
    "Each phase is an object: { name, intent (short), target_seconds, segments[] }, where target_seconds is "
    "your rough time budget for that phase (the six should sum to ~600). Put segments in the order they play. "
    "A segment is either:\n"
    '  { "type": "speech", "text": "...", "tone": "..." }   — a short spoken guidance, 1-3 sentences\n'
    '  { "type": "silence", "seconds": <integer 3-150> }    — a held pause of exactly that length\n'
    '  { "type": "silence", "seconds": "flex" }             — OPTIONAL: a pause the backend expands to fill the phase budget\n\n'
    "PACING — this is the craft:\n"
    "- Speak briefly, then hold. The natural rhythm is a short guidance followed by a long silence.\n"
    "- Use GENEROUS silences. 60, 90, even 120 seconds of stillness is normal and welcome — most of all in "
    "body_scan and core_practice, where the person is doing the inner work. arrival and breath_anchor may be "
    "a little more spoken; the deep middle should be mostly silent.\n"
    "- A phase can be almost all silence with a single spoken cue — or, occasionally, need no new words at all.\n"
    "- Across the WHOLE sit keep it to roughly 6-10 spoken moments (never more than 12). Sparse on purpose.\n"
    '- Set real silence lengths yourself; reach for a single "flex" in a phase only when you want the backend '
    "to fill that phase's remaining time for you.\n\n"
    "HARD RULES (the manifest is rejected otherwise):\n"
    "- The closing phase must END on a speech segment, so the sit lands rather than trailing into silence.\n"
    "- Speech text is short (1-3 sentences), calm, second-person, present-tense. Never read instructions aloud.\n"
    '- Always address them as "you". NEVER use a name or any [BRACKETED_TOKEN] — those are voiced literally by TTS.\n'
    "- core_practice is the personalized heart: gently name what the signals suggest they're carrying, and "
    "offer permission to set it down. Be specific but kind; never clinical, never list their data back.\n"
    "- closing gives one small carry-forward intention.\n\n"
    "Return ONLY the JSON object — no prose, no markdown fences."
)


def _format_signals(signals: dict) -> str:
    """Render the gathered signals as a compact prompt context (no raw PII dumps)."""
    lines: list[str] = ["Here is what you've gathered about this person's recent days:"]
    themes = signals.get("recent_themes") or []
    if themes:
        lines.append("Recent journal themes:")
        lines.extend(f"- {t}" for t in themes[:8])
    notes = signals.get("recent_notes") or []
    if notes:
        lines.append("Recent daily-note snippets:")
        lines.extend(f"- {n}" for n in notes[:6])
    if signals.get("last_meditation_theme"):
        lines.append(f"Their last meditation's theme (vary from it): {signals['last_meditation_theme']}")
    if signals.get("additional_context"):
        lines.append(f"What they've asked to keep in mind: {signals['additional_context']}")
    if len(lines) == 1:
        lines.append(
            "- (little specific signal this week — compose a gentle, universal sit about arriving, "
            "breathing, and setting down whatever today held)"
        )
    lines.append(
        "\nCompose today's sit now — holistically. Let silence carry most of the ten minutes; speak only "
        "where it guides. Rough per-phase budget to pace toward (yours to adjust): "
        + ", ".join(f"{k}~{v}s" for k, v in _PHASE_TARGETS.items())
        + "."
    )
    return "\n".join(lines)


def _normalize(manifest: dict, voice: str) -> dict:
    """Backstop the scaffold fields the model must not drift on, before validation.

    The per-phase time budgets are now the model's to allocate (holistic pacing) —
    we only fill a sane default when it omits one, so the renderer always has a
    positive ``target_seconds`` for any flex silence to resolve against.
    """
    if not isinstance(manifest, dict):
        raise ComposeError("LLM did not return a JSON object")
    manifest.setdefault("schema_version", 1)
    manifest["voice"] = voice or manifest.get("voice") or render.DEFAULT_VOICE
    manifest.setdefault("global_tone", "soft, slow, warm; unhurried with generous space")
    manifest.setdefault("total_target_seconds", 600)
    manifest.setdefault("ambient", None)
    for phase in manifest.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        try:
            if float(phase.get("target_seconds")) > 0:
                continue  # keep the model's allocation
        except (TypeError, ValueError):
            pass
        phase["target_seconds"] = _PHASE_TARGETS.get(phase.get("name"), 60)
    return manifest


def author_manifest(signals: dict, *, voice: str = "", model: str = "") -> dict:
    """Author a validated render manifest from raw signals via the LLM.

    Raises ``ComposeError`` if the key is missing, the LLM call fails, the output
    isn't parseable JSON, or the manifest fails ``render.validate_manifest``.
    """
    api_key = getattr(settings, "OPENROUTER_API_KEY", "") or ""
    if not api_key:
        raise ComposeError("OPENROUTER_API_KEY not configured")
    model = model or getattr(settings, "CORE_COMPOSE_MODEL", "") or DEFAULT_COMPOSE_MODEL

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _format_signals(signals)},
                ],
                "max_tokens": _MAX_OUTPUT_TOKENS,
                "temperature": 0.7,
                "response_format": {"type": "json_object"},
            },
            timeout=_LLM_TIMEOUT_S,
        )
        resp.raise_for_status()
        content = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        raise ComposeError(f"LLM call failed: {str(exc)[:200]}") from exc

    try:
        manifest = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ComposeError(f"LLM returned non-JSON: {content[:160]}") from exc

    manifest = _normalize(manifest, voice)
    errors = render.validate_manifest(manifest)
    if errors:
        raise ComposeError("authored manifest invalid: " + "; ".join(errors[:4]))
    return manifest
