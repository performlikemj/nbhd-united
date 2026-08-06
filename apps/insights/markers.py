"""Extract and record assistant insights from outbound reply markup.

The assistant emits ``[[insight:topic_slug]]statement[[/insight]]`` markers
inline in its replies (see ``templates/openclaw/rules/reply-markers.md``).
The topic spec optionally carries a pillar prefix —
``[[insight:<pillar>/<slug>]]`` (e.g. ``[[insight:fuel/sleep_quality]]``) —
so a single fleet-wide reply path can file insights against the right pillar
instead of forcing everything into one. This module:

1. Finds every marker in the outbound text.
2. Parses an optional ``<pillar>/`` prefix off the topic spec. A recognised
   canonical pillar wins; anything else (missing prefix, unknown pillar name,
   stray ``/``) falls back safely to the caller-supplied ``pillar`` default.
3. Enforces the Gravity kill switch: a marker that resolves to the ``gravity``
   pillar is only ever persisted for a tenant with ``finance_active`` true.
   ``rules/reply-markers.md`` teaches the gravity taxonomy to every assistant
   fleet-wide, so a ``gravity/``-prefixed marker can arrive for a tenant who
   never opted into finance; ``Tenant.finance_active`` is the authoritative
   kill switch (mirrors the ``_OBSERVATION_GATE`` gate in
   ``apps.insights.envelope``). Such a marker is refused — the statement stays
   visible, but no structured financial-life row is written.
4. Resolves the ``slug`` via ``apps.insights.topic_resolver`` under the chosen
   pillar. An unknown slug becomes a ``proposed`` ``TopicRegistry`` row, same
   path the agent would take through ``nbhd_insights_record`` with a novel slug.
5. Writes an ``AssistantInsight`` row with ``status='open'``.
6. Returns the text with the marker tokens stripped — the statement
   stays in the user-facing reply; only ``[[insight:...]]`` and
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
from apps.insights.pillars import Pillar
from apps.insights.topic_resolver import resolve_topic
from apps.pii.egress import redact_known_values

logger = logging.getLogger(__name__)

# Canonical pillar set — the authoritative enum, not a hand-maintained list.
_CANONICAL_PILLARS = frozenset(Pillar.values)

# Neutral default when neither the marker nor the caller names a pillar.
# Deliberately NOT "gravity": a fleet-wide reply marker with no pillar segment
# is far more likely to be about the user's life/journal context than finance,
# and misfiling everything as gravity was the bug this parsing fixes.
DEFAULT_MARKER_PILLAR = Pillar.JOURNAL.value

# Marker syntax: [[insight:<pillar>/<slug>]]statement[[/insight]] where the
# ``<pillar>/`` prefix is optional.
# - topic spec: any non-bracket text — resolve_topic handles slugification and
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


def _split_pillar_slug(raw: str, *, default_pillar: str) -> tuple[str, str]:
    """Split a marker topic spec into ``(pillar, slug)``.

    Supports the pillar-qualified form ``pillar/slug``. When the segment before
    the first ``/`` is a canonical pillar, that pillar wins and the remainder is
    the slug. Otherwise the whole string is the slug under ``default_pillar`` —
    so a stray ``/`` or an unknown pillar name never *misfiles* the insight; it
    just becomes part of the proposed topic that ops can reassign later.
    """
    default = default_pillar if default_pillar in _CANONICAL_PILLARS else DEFAULT_MARKER_PILLAR
    if "/" in raw:
        head, _, tail = raw.partition("/")
        head = head.strip().lower()
        tail = tail.strip()
        if head in _CANONICAL_PILLARS and tail:
            return head, tail
    return default, raw


def extract_and_record_insights(
    text: str,
    *,
    tenant,
    pillar: str = DEFAULT_MARKER_PILLAR,
) -> str:
    """Extract every insight marker from ``text``, write rows, return cleaned text.

    Thin wrapper over :func:`extract_and_record_insights_with_ids` that discards
    the created-id list — the common case (every live reply path) only needs the
    cleaned user-facing text back.
    """
    cleaned, _created = extract_and_record_insights_with_ids(text, tenant=tenant, pillar=pillar)
    return cleaned


def extract_and_record_insights_with_ids(
    text: str,
    *,
    tenant,
    pillar: str = DEFAULT_MARKER_PILLAR,
) -> tuple[str, list[str]]:
    """Extract every insight marker, write rows, return ``(cleaned_text, ids)``.

    ``ids`` is the list of ``AssistantInsight`` primary keys this call actually
    created, in document order. A caller that needs to attribute *exactly* the
    insights it birthed (e.g. the weekly reflection recording its own
    ``insight_id``) must use these ids rather than a post-hoc "latest open row
    for tenant" query — that query races any concurrent live reply path writing
    an insight for the same tenant.

    ``pillar`` is the fallback pillar for markers that don't carry their own
    ``<pillar>/`` prefix. It defaults to ``journal`` (a neutral, always-on
    pillar) so a generic chat reply path no longer misfiles every observation
    as ``gravity``. Callers with a known context (e.g. the Gravity weekly
    reflection) pass an explicit ``pillar=``; a marker's own prefix always wins
    over this default.

    Gravity kill switch: a marker that resolves to the ``gravity`` pillar is
    refused (no row written, id not returned) unless ``tenant.finance_active``
    is true. The statement still stays in the returned text.

    Failure handling: any individual marker that fails to record (DB error,
    topic-resolver exception) is logged and stripped from the text without
    blocking the others. The user-facing reply must go out regardless of
    bookkeeping success.
    """
    if not text or "[[insight:" not in text:
        return text, []

    created_ids: list[str] = []
    finance_active = bool(getattr(tenant, "finance_active", False))

    def _replace(match: re.Match[str]) -> str:
        raw = (match.group(1) or "").strip()
        statement = (match.group(2) or "").strip()
        if not raw or not statement:
            # Malformed marker — just strip silently. Logging would be noisy
            # if the agent occasionally writes a placeholder.
            return statement
        if len(statement) > _MAX_STATEMENT_LEN:
            statement = statement[:_MAX_STATEMENT_LEN].rstrip()

        marker_pillar, slug = _split_pillar_slug(raw, default_pillar=pillar)

        # Gravity kill switch. finance_active is the authoritative gate for all
        # Gravity data (Tenant.finance_active); the fleet-wide reply-markers doc
        # teaches the gravity taxonomy to every assistant, so a gravity marker
        # can surface for a tenant who never opted into finance. Refuse the
        # write — keep the statement visible, persist nothing structured.
        if marker_pillar == Pillar.GRAVITY.value and not finance_active:
            logger.info(
                "insight marker refused: gravity write for non-finance tenant (tenant=%s slug=%s)",
                str(getattr(tenant, "id", "?"))[:8],
                slug,
            )
            return statement

        try:
            topic = resolve_topic(marker_pillar, slug)
            # Some owner delivery paths rehydrate before marker extraction. Guard
            # only the persisted copy so storage stays placeholder-space while the
            # returned statement keeps its owner-visible delivery behavior.
            stored_statement = redact_known_values(
                tenant,
                statement,
                seam="insight_marker_storage",
            )
            insight = AssistantInsight.objects.create(
                tenant=tenant,
                pillar=marker_pillar,
                topic=topic,
                statement=stored_statement,
                status=AssistantInsight.Status.OPEN,
            )
            created_ids.append(str(insight.id))
        except Exception:
            logger.exception(
                "insight marker recording failed (tenant=%s pillar=%s slug=%s)",
                str(getattr(tenant, "id", "?"))[:8],
                marker_pillar,
                slug,
            )
        # Always strip the marker tokens, regardless of write success.
        # User-visible text is just the statement.
        return statement

    cleaned = INSIGHT_MARKER_RE.sub(_replace, text)
    return cleaned, created_ids
