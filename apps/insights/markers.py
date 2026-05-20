"""Extract and record assistant insights from outbound reply markup.

The assistant emits ``[[insight:topic_slug]]statement[[/insight]]`` markers
inline in its replies (see ``templates/openclaw/rules/reply-markers.md``).
This module:

1. Finds every marker in the outbound text.
2. Resolves ``topic_slug`` via ``apps.insights.topic_resolver``. An unknown
   slug becomes a ``proposed`` ``TopicRegistry`` row, same path the agent
   would take through ``nbhd_insights_record`` with a novel slug.
3. Writes an ``AssistantInsight`` row with ``status='open'``.
4. Returns the text with the marker tokens stripped — the statement
   stays in the user-facing reply; only ``[[insight:slug]]`` and
   ``[[/insight]]`` are removed.

This is invoked from every outbound message path (Telegram poller,
Telegram webhook drain, LINE webhook) — see feedback_all_channels memory.
The same text is never processed twice because each call site processes
the reply once on the way out; we don't need a transcript-level dedupe.
"""

from __future__ import annotations

import logging
import re

from apps.insights.models import AssistantInsight
from apps.insights.topic_resolver import resolve_topic

logger = logging.getLogger(__name__)

# Marker syntax: [[insight:slug_or_natural_string]]statement[[/insight]]
# - slug: any non-bracket text — resolve_topic handles slugification and
#   falls back to alias / proposed creation, so a "natural" string the
#   agent typed (e.g. "eating out") still resolves correctly.
# - statement: non-greedy across newlines (.*? + DOTALL) so multi-line
#   wrapped observations extract intact and empty statements still match.
INSIGHT_MARKER_RE = re.compile(
    r"\[\[insight:([^\]]+?)\]\](.*?)\[\[/insight\]\]",
    re.DOTALL,
)

# Maximum statement length we'll persist. AssistantInsight.statement is
# unbounded TextField, but anything past this is almost certainly the
# agent wrapping a whole paragraph that wasn't meant as a single insight.
_MAX_STATEMENT_LEN = 1000


def extract_and_record_insights(
    text: str,
    *,
    tenant,
    pillar: str = "gravity",
) -> str:
    """Extract every insight marker from ``text``, write rows, return cleaned text.

    ``pillar`` defaults to ``gravity`` because Phase 2 / Phase 3 only target
    that pillar today; markers in non-Gravity contexts still resolve and write
    but will need a ``pillar=`` override at the call site once Fuel / Core
    extend the rules.

    Failure handling: any individual marker that fails to record (DB error,
    topic-resolver exception) is logged and stripped from the text without
    blocking the others. The user-facing reply must go out regardless of
    bookkeeping success.
    """
    if not text or "[[insight:" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        slug = (match.group(1) or "").strip()
        statement = (match.group(2) or "").strip()
        if not slug or not statement:
            # Malformed marker — just strip silently. Logging would be noisy
            # if the agent occasionally writes a placeholder.
            return statement
        if len(statement) > _MAX_STATEMENT_LEN:
            statement = statement[:_MAX_STATEMENT_LEN].rstrip()

        try:
            topic = resolve_topic(pillar, slug)
            AssistantInsight.objects.create(
                tenant=tenant,
                pillar=pillar,
                topic=topic,
                statement=statement,
                status=AssistantInsight.Status.OPEN,
            )
        except Exception:
            logger.exception(
                "insight marker recording failed (tenant=%s slug=%s)",
                str(getattr(tenant, "id", "?"))[:8],
                slug,
            )
        # Always strip the marker tokens, regardless of write success.
        # User-visible text is just the statement.
        return statement

    return INSIGHT_MARKER_RE.sub(_replace, text)
