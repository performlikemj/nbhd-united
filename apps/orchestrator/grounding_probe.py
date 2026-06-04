"""Read-only "grounding probe": does a proactive cron have the recent ground truth?

A proactive cron (the "midweek pulse" / Project Check-in / etc.) runs in an
ISOLATED OpenClaw session, so it is grounded ONLY in structured state — the
always-loaded USER.md envelope (``render_managed_region``) plus whatever
documents it can reach via ``nbhd_journal_search`` / ``nbhd_document_get``. It
has no access to the live Telegram/LINE conversation.

This module renders that exact surface for a tenant + topic and reports whether
a *known-recent ground-truth fact* is present, and how fresh the sources are.
It is the test instrument for the proactive-grounding work: run it before a
change (gap reproduced → ``grounded=False``) and after (→ ``True``).

Read-only: no writes, no container calls. ``render_managed_region`` is the same
side-effect-free render used to build USER.md; the search replicates
``RuntimeJournalSearchView`` (the backend of the agent's ``nbhd_journal_search``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from apps.journal.models import Document


@dataclass
class GroundingReport:
    topic: str
    expect_terms: list[str]
    grounded: bool  # every expected term reachable (or, with no terms, any doc surfaces)
    topic_in_envelope: bool  # topic surfaces in the always-loaded USER.md envelope
    term_in_envelope: dict[str, bool]  # per term: present in the always-loaded envelope
    term_reachable: dict[str, bool]  # per term: in envelope OR any reachable doc's markdown
    term_in_project: dict[str, bool]  # per term: present in a reachable kind='project' doc (canonical target)
    grounded_in_project: bool  # every term in a project doc — catches misfiles to daily/other kinds
    reachable_docs: list[dict]  # docs the topic surfaces (kind/slug/updated_at/rank)
    newest_source: datetime | None
    envelope_error: str | None = None


def journal_search(tenant, query: str, limit: int = 20) -> list[Document]:
    """Replicate ``RuntimeJournalSearchView`` — the backend of ``nbhd_journal_search``."""
    from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

    search_vector = SearchVector("title", weight="A") + SearchVector("markdown", weight="B")
    search_query = SearchQuery(query, search_type="websearch")
    return list(
        Document.objects.filter(tenant=tenant)
        .annotate(rank=SearchRank(search_vector, search_query))
        .filter(rank__gt=0.0)
        .order_by("-rank")[:limit]
    )


def probe_grounding(tenant, topic: str, expect_terms: list[str] | None = None) -> GroundingReport:
    """Assemble the structured-state context a proactive cron would see for ``topic``.

    ``expect_terms`` are ground-truth substrings that SHOULD be reachable (e.g.
    the substance of a recent conversation). ``grounded`` is True only if every
    one is present in the always-loaded envelope or in a reachable document.
    """
    terms = [t.strip() for t in (expect_terms or []) if t and t.strip()]

    # 1. Always-loaded USER.md envelope (side-effect-free render). Never let a
    #    render hiccup fail the probe — fall back to doc-driven reachability.
    envelope = ""
    envelope_error: str | None = None
    try:
        from apps.orchestrator.workspace_envelope import render_managed_region

        envelope = render_managed_region(tenant) or ""
    except Exception as exc:  # pragma: no cover - defensive
        envelope_error = f"{type(exc).__name__}: {exc}"
    envelope_l = envelope.lower()

    # 2. Documents the cron could reach for this topic: full-text search
    #    (nbhd_journal_search) UNION a literal phrase match (what a
    #    nbhd_document_get would surface). Full markdown is the BEST CASE the
    #    agent could ground on — if a term is absent here, no tool call could
    #    surface it, so the cron is definitively ungrounded on it.
    reachable: dict = {}
    for doc in journal_search(tenant, topic):
        reachable[doc.id] = doc
    for doc in Document.objects.filter(tenant=tenant, markdown__icontains=topic):
        reachable.setdefault(doc.id, doc)
    docs = sorted(reachable.values(), key=lambda d: d.updated_at, reverse=True)

    reachable_blob = "\n".join((d.markdown or "") for d in docs).lower()

    # Canonical-target check: is the substance in a PROJECT doc specifically?
    # A status update misfiled to a daily doc is still "reachable" (journal
    # search finds it) but leaves the canonical project doc stale — this
    # dimension catches that misfile.
    project_blob = "\n".join((d.markdown or "") for d in docs if d.kind == "project").lower()

    term_in_envelope = {t: (t.lower() in envelope_l) for t in terms}
    term_reachable = {t: (t.lower() in envelope_l or t.lower() in reachable_blob) for t in terms}
    term_in_project = {t: (t.lower() in project_blob) for t in terms}

    return GroundingReport(
        topic=topic,
        expect_terms=terms,
        grounded=(all(term_reachable.values()) if terms else bool(docs)),
        grounded_in_project=(all(term_in_project.values()) if terms else any(d.kind == "project" for d in docs)),
        topic_in_envelope=(topic.lower() in envelope_l),
        term_in_envelope=term_in_envelope,
        term_reachable=term_reachable,
        term_in_project=term_in_project,
        reachable_docs=[
            {
                "kind": d.kind,
                "slug": d.slug,
                "updated_at": d.updated_at,
                "rank": float(getattr(d, "rank", 0.0) or 0.0),
            }
            for d in docs
        ],
        newest_source=(docs[0].updated_at if docs else None),
        envelope_error=envelope_error,
    )
