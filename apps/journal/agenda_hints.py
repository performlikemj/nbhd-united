"""Cross-domain agenda-hint extractor (Phase C).

Runs as a second pass after the existing extraction pipeline. Given:

- Today's journal text (already in hand from the main extractor)
- The tenant's currently-eligible open agenda threads (from
  ``apps.orchestrator.agenda_threads.open_threads``)

Asks an LLM to identify which threads were *mentioned* in the journal
and how the user related to them — warm (wants to engage), redirect
(actively avoiding), ignore (touched but neutral), organic (user
already engaging without prompting). Each match becomes a
``record_signal`` call against the underlying ``AgendaEngagement`` row.

This is the cross-domain glue: when the user writes about money
stress in their journal, the dormant Gravity intro becomes more
salient because the classifier emits a 'warm' signal against it. The
agenda renderer (Phase B's eligibility filter) reads the signal log
on the next render and behaves accordingly.

Defensive shape:
- LLM-call failure → log + return {} → main extraction is unaffected
- Output JSON malformed → log + return {} → no signals written
- Thread the classifier mentions but isn't in the input set → ignored
- Per-signal write errors → logged + continue (don't lose the rest)
"""

from __future__ import annotations

import json
import logging
import re

from apps.billing.constants import DEEPSEEK_FLASH_MODEL, GEMMA_MODEL
from apps.common.openrouter import chat_completion
from apps.tenants.agenda_models import AgendaEngagement
from apps.tenants.agenda_service import mark_organic, record_signal
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


HINT_MODEL = "openai/gpt-4o-mini"
# Lightweight classification — fall back to the cheap chat models if gpt-4o-mini
# is unreachable on OpenRouter rather than dropping agenda hints for the run.
HINT_MODELS = [HINT_MODEL, GEMMA_MODEL, DEEPSEEK_FLASH_MODEL]
HINT_TIMEOUT = 30
HINT_MAX_CONTENT_CHARS = 6000

_VALID_SIGNALS = {"warm", "redirect", "ignore", "organic"}


_HINT_SYSTEM_PROMPT = """\
You read a user's daily journal. Given a list of open threads the
assistant is tracking with this user, identify which threads (if any)
the user mentioned in the journal — and classify how they related to
each one.

Return ONLY valid JSON matching this schema:
{
  "matches": [
    {"kind": "<thread kind>", "item_id": "<thread item_id>", "signal": "warm|redirect|ignore|organic"}
  ]
}

Signals:
- "warm": user expressed interest, motivation, or readiness to engage with this thread
- "redirect": user explicitly avoided, deferred, or pushed back on this topic
- "ignore": topic was touched but neutrally — no clear engagement signal
- "organic": user is already actively engaging with this without needing assistant prompting

Rules:
- Only match threads that are explicitly or strongly implicitly mentioned. Don't infer.
- Use the exact ``kind`` and ``item_id`` from the threads list — never invent.
- Empty matches array is valid — return {"matches": []} if no threads were mentioned.
- One thread can only appear once per pass; if the journal sends mixed signals, pick the strongest.
"""


def run_agenda_hint_pass(tenant: Tenant, journal_content: str) -> dict[str, int]:
    """Run the cross-domain hint pass for one tenant.

    Returns a small summary dict for logging / metrics:
    ``{"matches": N, "warm": N, "redirect": N, ...}``. Always returns —
    never raises. Caller is the existing extraction pipeline; a hint
    pass failure must not interfere with main extraction.
    """
    from apps.orchestrator.agenda_threads import open_threads

    summary = {"matches": 0, "warm": 0, "redirect": 0, "ignore": 0, "organic": 0}

    if not journal_content or len(journal_content) < 50:
        return summary

    threads = open_threads(tenant)
    if not threads:
        return summary

    try:
        matches = _classify(journal_content, threads)
    except Exception:
        logger.exception(
            "agenda_hints: classifier call failed for tenant %s",
            str(tenant.id)[:8],
        )
        return summary

    valid_thread_keys = {(t.kind, t.item_id) for t in threads}

    for match in matches:
        kind = (match.get("kind") or "").strip()
        item_id = (match.get("item_id") or "").strip()
        signal = (match.get("signal") or "").strip().lower()

        if (kind, item_id) not in valid_thread_keys:
            # Classifier hallucinated a thread — drop silently.
            continue
        if signal not in _VALID_SIGNALS:
            continue

        try:
            record_signal(tenant, kind=kind, item_id=item_id, signal=signal)
            # Phase D: an 'organic' signal on an assistant commitment
            # means the user re-raised the committed topic before the
            # assistant did — the state machine moves to ACTIVE so the
            # assistant supports rather than introduces.
            if signal == "organic" and kind == AgendaEngagement.Kind.ASSISTANT_COMMITMENT:
                mark_organic(tenant, kind=kind, item_id=item_id)
        except Exception:
            logger.exception(
                "agenda_hints: failed to record signal for tenant=%s kind=%s item=%s",
                str(tenant.id)[:8],
                kind,
                item_id,
            )
            continue

        summary["matches"] += 1
        summary[signal] = summary.get(signal, 0) + 1

    if summary["matches"]:
        logger.info(
            "agenda_hints: tenant=%s matches=%d (warm=%d redirect=%d ignore=%d organic=%d)",
            str(tenant.id)[:8],
            summary["matches"],
            summary["warm"],
            summary["redirect"],
            summary["ignore"],
            summary["organic"],
        )

    return summary


def _classify(content: str, threads) -> list[dict]:
    """Single LLM call. Raises on transport / parse failure — caller
    catches and logs."""
    threads_block = "\n".join(
        f"- kind={t.kind}, item_id={t.item_id}, label={t.label!r}" + (f", context={t.context!r}" if t.context else "")
        for t in threads
    )

    user_prompt = (
        "Open threads being tracked with this user:\n"
        f"{threads_block}\n\n"
        "Journal text:\n"
        f"{content[:HINT_MAX_CONTENT_CHARS]}"
    )

    data, _model_used = chat_completion(
        HINT_MODELS,
        [
            {"role": "system", "content": _HINT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        timeout=HINT_TIMEOUT,
    )
    raw = (data["choices"][0]["message"]["content"] or "").strip()
    # Strip markdown fences if the provider wrapped JSON
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    parsed = json.loads(raw)
    matches = parsed.get("matches", [])
    if not isinstance(matches, list):
        return []
    return matches
