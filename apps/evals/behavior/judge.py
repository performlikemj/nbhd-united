"""Behavior soft-dimension judge — pinned model, versioned rubric, spend-capped.

Backend computes evidence; the LLM only JUDGES the subjective dimensions
(helpfulness, warmth, boundary). The judge is:

  * PINNED — ``JUDGE_MODEL`` is Claude Sonnet 5 via OpenRouter, a single constant,
    so scores are comparable over time. Swapping it (or the rubric) is a deliberate
    version bump, and every score row is stamped with ``judge_model`` +
    ``rubric_version`` so a trend query never silently blends two judges.
  * SPEND-CAPPED — a hard ``JUDGE_MAX_TOKENS`` output cap per call, plus a per-run
    scenario cap enforced by the suite. Combined with the behavior tenant's own
    monthly OpenRouter ceiling, this bounds judge spend.
  * SANDBOXED to synthetic content — the judge sees ONLY the synthetic scenario
    persona + transcript + the rubric (INVARIANT #1). It returns a 1-5 score and a
    short rationale per dimension; the rationale QUOTES the transcript, so it is
    used transiently and then DISCARDED — never persisted, never logged.

Uses the platform's shared OpenRouter client (``apps.common.openrouter``), the same
call idiom as ``model_health`` and the Gravity/journal callers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from django.conf import settings

from apps.evals.behavior.rubrics import rubric_v1

logger = logging.getLogger(__name__)

# Pinned judge — Claude Sonnet 5 via OpenRouter (docs/evals-directive.md §3). The
# ``anthropic/`` prefix is the bare OpenRouter slug; the shared client's
# ``normalize_model_id`` leaves it untouched (only ``openrouter/`` is stripped).
JUDGE_MODEL = "anthropic/claude-sonnet-5"
RUBRIC_VERSION = rubric_v1.RUBRIC_VERSION

# Spend caps. ``JUDGE_MAX_TOKENS`` bounds each call's output; the suite bounds how
# many scenarios per run reach the judge. The timeout also feeds the suite's
# worst-case wall-clock budget (SUITE_BUDGET_SECONDS): 30s is generous for one
# ~700-output-token JSON scoring call, and a slower judge is simply recorded
# skipped-with-reason ``judge_error`` (advisory) — never worth risking the 300s
# worker ceiling for.
JUDGE_MAX_TOKENS = 700
JUDGE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class JudgeScore:
    """One dimension's outcome. ``ok=False`` means 'record this soft dimension as
    SKIPPED with ``reason``' — never a fabricated score."""

    dimension: str
    score: int | None
    ok: bool
    reason: str = ""  # short code when ok is False, e.g. "parse_error"


class Judge(Protocol):
    """Scores soft dimensions from SYNTHETIC scenario content only."""

    model: str
    rubric_version: str

    def score(
        self, *, scenario_id: str, persona: str, transcript_lines: list[tuple[str, str]], dimensions: list[str]
    ) -> dict[str, JudgeScore]: ...


def _clamp_score(raw: object) -> int | None:
    try:
        value = int(raw)  # tolerate "4" / 4.0
    except (TypeError, ValueError):
        return None
    if value < 1 or value > 5:
        return None
    return value


class OpenRouterJudge:
    """Production judge: one capped OpenRouter call per scenario, rubric v1."""

    model = JUDGE_MODEL
    rubric_version = RUBRIC_VERSION

    def score(
        self, *, scenario_id: str, persona: str, transcript_lines: list[tuple[str, str]], dimensions: list[str]
    ) -> dict[str, JudgeScore]:
        # Local import keeps the openrouter dependency out of module import time and
        # lets tests that inject a fake judge avoid it entirely.
        from apps.common.openrouter import chat_completion

        prompt = rubric_v1.build_judge_prompt(persona=persona, dimensions=dimensions, transcript_lines=transcript_lines)
        messages = [
            {"role": "system", "content": rubric_v1.JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        try:
            data, _model_used = chat_completion(
                JUDGE_MODEL,
                messages,
                max_tokens=JUDGE_MAX_TOKENS,
                timeout=JUDGE_TIMEOUT_SECONDS,
                record_health=False,
                response_format={"type": "json_object"},
            )
        except Exception:  # noqa: BLE001 — a judge failure is ADVISORY, never a run failure
            logger.warning("behavior judge: OpenRouter call failed for scenario %s", scenario_id)
            return {d: JudgeScore(d, None, ok=False, reason="judge_error") for d in dimensions}

        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("judge did not return a JSON object")
        except (json.JSONDecodeError, ValueError):
            logger.warning("behavior judge: unparseable JSON for scenario %s", scenario_id)
            return {d: JudgeScore(d, None, ok=False, reason="parse_error") for d in dimensions}

        # NOTE: we read ONLY the integer score per dimension. The rationale is
        # deliberately DROPPED here (it quotes the transcript) — never stored, never
        # logged (INVARIANT #1).
        out: dict[str, JudgeScore] = {}
        for dim in dimensions:
            entry = parsed.get(dim)
            raw_score = entry.get("score") if isinstance(entry, dict) else entry
            score = _clamp_score(raw_score)
            if score is None:
                out[dim] = JudgeScore(dim, None, ok=False, reason="missing_dimension")
            else:
                out[dim] = JudgeScore(dim, score, ok=True)
        return out


def build_default_judge() -> Judge | None:
    """The production judge, or ``None`` when OpenRouter is unconfigured.

    Returning ``None`` (not a stub that fabricates scores) is deliberate: the suite
    records the soft dimensions as SKIPPED with reason ``judge_unconfigured`` — never
    silently, never green-by-default (docs/evals-directive.md §Suite 2, item 4).
    """
    if not (getattr(settings, "OPENROUTER_API_KEY", "") or ""):
        logger.info("behavior judge: OPENROUTER_API_KEY unset — soft dimensions will be skipped-with-reason")
        return None
    return OpenRouterJudge()
