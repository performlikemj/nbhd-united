"""Core services — meditation signal gathering and the render pipeline.

Split per the project invariant "backend computes evidence, LLM makes judgments":
``gather_meditation_signals`` returns RAW signals (no scores/formulas); the
assistant weighs them into a render manifest; ``render_meditation`` is the
deterministic executor. The render pipeline (segment-and-stitch via Gemini TTS +
ffmpeg, with bounded-parallel + per-call timeout + retry + non-fatal fallback)
lands in Phase 1.
"""

from __future__ import annotations

import logging

from apps.core.models import MeditationSession
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def gather_meditation_signals(tenant: Tenant) -> dict:
    """Raw signals the assistant draws on to compose today's meditation.

    Phase 1 enriches this with insights ``yesterdays_signals``, journal goals /
    open tasks / recent daily-note themes, and fuel/finance baselines. Returns
    raw evidence only — the LLM (not a backend formula) decides tone and content.
    """
    # TODO(Phase 1): pull insights + journal + fuel/finance signals.
    return {"tenant_id": str(tenant.id)}


def render_meditation(session: MeditationSession) -> None:
    """Render a session's manifest to audio and flip it to ready (Phase 1).

    Pipeline: render each narration segment via Gemini TTS (bounded-parallel,
    per-call timeout, retry, non-fatal silence fallback) → insert exact silences
    with ffmpeg → stitch → transcode (mp3 + ogg) → store on the per-tenant share
    → set audio_url / ogg_url / duration_ms → status=ready → notify linked
    channel. Apply the finance idempotency guard: reuse an existing ``ready``
    session for the same date rather than re-rendering.
    """
    raise NotImplementedError("Core render pipeline lands in Phase 1")
