"""Receipt bookkeeping shared by every Document-family write and owner read.

The chokepoint itself lives in :mod:`apps.pii.authoring`; this module holds the
Document-shaped glue around it — which is entirely about one asymmetry:

* A **full-field write** (POST create, PATCH, runtime PUT, memory PUT) replaces
  the whole column, so the authoring receipt for the submitted text IS the
  field's receipt. Use :func:`set_field_receipt`.
* An **append / section write** authors only the new FRAGMENT — re-running NER
  over an entire daily note on every quick-log would cost a full inference per
  append for no new information. But the stored receipt describes the whole
  column, so the fragment's receipt has to be folded into what is already
  there. Use :func:`merge_field_receipt`.

Folding is deliberately pessimistic (:data:`_STATE_RANK`): the merged state is
never BETTER than either contributor. Letting a clean fragment upgrade a field
whose older half was never checked would tell the A7 migration fence the field
is already verified and skip it forever — the exact unsoundness review P1-3
removed from row-presence fencing.
"""

from __future__ import annotations

import re
from typing import Any

# Worst → best. ``unconfirmed`` and ``residual`` both pull a field into the
# repair sweep; ``bypass`` means no checked provenance ran at all, which is
# worse than a verified ``placeholder`` but is NOT a repair target. A field with
# no prior receipt is legacy text, so it enters the fold as ``bypass``: honest
# about having never been checked, without inventing a repair job for every
# pre-P3 row an append happens to touch.
_STATE_RANK = {"unconfirmed": 0, "residual": 1, "bypass": 2, "placeholder": 3}
_LEGACY_RANK = _STATE_RANK["bypass"]


def set_field_receipt(receipts: Any, field: str, receipt: dict[str, Any]) -> dict[str, Any]:
    """Return ``receipts`` with ``field`` replaced — for whole-column writes."""
    out = dict(receipts or {})
    out[field] = receipt
    return out


def merge_field_receipt(
    receipts: Any,
    field: str,
    receipt: dict[str, Any],
    *,
    stored_text: str,
) -> dict[str, Any]:
    """Fold a fragment's receipt into ``field``'s whole-column receipt.

    ``stored_text`` is the FINAL column value (old text plus the new fragment):
    the redaction list is rebuilt from it so the owner's purple affordances
    cover the whole document, not just the last append.
    """
    out = dict(receipts or {})
    prior = out.get(field)
    prior_state = prior.get("state") if isinstance(prior, dict) else None
    prior_rank = _STATE_RANK.get(prior_state, _LEGACY_RANK)
    new_rank = _STATE_RANK.get(receipt.get("state"), _LEGACY_RANK)

    winner = receipt if new_rank <= prior_rank else prior
    if isinstance(winner, dict):
        merged = dict(winner)
    else:
        # No prior receipt at all: the older half of this column is pre-P3
        # legacy text that nothing ever checked, and it just lost the fold to
        # the fragment. Say so explicitly instead of returning a receipt with no
        # state, which would decode as "legacy" and lose the fragment's writer.
        merged = {"state": "bypass", "reason": "legacy-body"}
    # ``writer`` describes the most recent authoring pass, so the appender wins
    # it even when the older half's state carries the day.
    if receipt.get("writer"):
        merged["writer"] = receipt["writer"]

    if merged.get("state") == "bypass":
        # A2: bypass receipts carry no placeholder data — nothing checked them.
        merged.pop("redactions", None)
    else:
        from apps.pii.authoring import receipt_placeholders

        merged["redactions"] = receipt_placeholders(stored_text)

    out[field] = merged
    return out


def get_or_create_authored_document(
    tenant,
    *,
    kind: str,
    slug: str,
    title: str,
    markdown_factory,
    seam: str,
):
    """Get a document, authoring its DEFAULT body when one has to be created.

    The default body is not clean by construction: for a daily note it is
    rendered from the tenant's own ``NoteTemplate`` sections, which the owner can
    rename to anything — including a person. Creating the row without authoring
    it leaves an unchecked half in the column forever, and
    :func:`merge_field_receipt` then (correctly) refuses to let any later append
    call the field verified. The flagship append surface would carry a permanent
    ``bypass`` receipt and the owner would get no entity affordances on it.

    Always the BACKGROUND writer class, never the calling seam's. A rendered
    template is server-composed text, not something a human just typed, so the
    owner class would be a lie about provenance — and an expensive one: at
    flag-off, owner means the legacy redactor, which is real detection and real
    minting on a path that had neither before P3. A4 requires flag-off to change
    nothing, and background is the only class that is a pure passthrough there.
    Flag-on it gets full detection plus MINT_VALIDATED, with anything unmintable
    recorded as ``residual`` for the repair sweep.

    The row is looked up BEFORE the body is rendered or authored so the common
    path — the document already exists — costs one SELECT and no NER, rather than
    a full inference on every append and every agent read.
    """
    from apps.journal.models import Document
    from apps.pii.authoring import author_text

    existing = Document.objects.filter(tenant=tenant, kind=kind, slug=slug).first()
    if existing is not None:
        return existing, False

    authored = author_text(
        tenant,
        markdown_factory(),
        seam=seam,
        writer="background",
        field="markdown",
        model_label="journal.Document",
    )
    return Document.objects.get_or_create(
        tenant=tenant,
        kind=kind,
        slug=slug,
        defaults={
            "title": title,
            "markdown": authored.text,
            "pii_receipts": {"markdown": authored.receipt},
        },
    )


def refresh_field_redactions(receipts: Any, field: str, stored_text: str) -> dict[str, Any]:
    """Re-derive ``field``'s redaction list from ``stored_text``, keeping state.

    For writes that REMOVE text (an undo scrubbing an approved entry back out of
    a document): the provenance state is unchanged — nothing new was authored —
    but placeholders that left with the removed block must leave the receipt too,
    or the owner keeps getting affordances for entities the document no longer
    mentions.
    """
    out = dict(receipts or {})
    receipt = out.get(field)
    if not isinstance(receipt, dict) or "redactions" not in receipt:
        return out

    from apps.pii.authoring import receipt_placeholders

    out[field] = {**receipt, "redactions": receipt_placeholders(stored_text)}
    return out


def owner_receipts(instance, tenant) -> dict[str, Any]:
    """Resolve a row's stored receipts against the owner's live entity map."""
    from apps.pii.authoring import resolve_receipt_values

    return resolve_receipt_values(
        getattr(instance, "pii_receipts", None) or {},
        getattr(tenant, "pii_entity_map", None),
    )


def owner_receipts_active(instance, tenant) -> dict[str, Any]:
    """``owner_receipts`` resolved against ACTIVE bindings only.

    The editor counterpart to :func:`rehydrate_active`, and it exists so the two
    halves of one response agree. With the full map, a retired placeholder still
    resolves to its name, so the body would show the literal ``[PERSON_2]`` while
    the receipt beside it carried ``value: "Bob"`` — the client would render a
    named entity chip over a token the owner deleted. Dropping the value key
    leaves an unresolved placeholder, which is exactly what the body shows.
    """
    from apps.pii.authoring import resolve_receipt_values

    return resolve_receipt_values(
        getattr(instance, "pii_receipts", None) or {},
        _active_bindings(tenant),
    )


# Matches the canonical ``[TYPE_N]`` shape from ``apps.pii.redactor``. TYPE may
# itself contain underscores (EMAIL_ADDRESS, CREDIT_CARD, IP_ADDRESS), so the
# character class has to include ``_`` — a ``[A-Z]+`` class silently skips every
# multi-word type and leaves exactly those variants unquoted.
#
# A deliberate SUBSET of ``redactor._PLACEHOLDER_RE``: that one also accepts an
# optional ``|label`` suffix (``[PERSON_4|Alice]``), this one does not. Query
# variants are built by substituting placeholders straight out of
# ``inverted_names_multimap``, which yields bare tokens — a labelled token never
# reaches here. Do NOT copy this pattern to anything that reads STORED text,
# where labelled tokens do occur.
_PLACEHOLDER_TOKEN_RE = re.compile(r"\[[A-Z_]+_\d+\]")


def as_fts_phrase(variant: str) -> str:
    """Quote every ``[TYPE_N]`` token in a websearch query variant.

    A placeholder is not one lexeme: ``[PERSON_4]`` parses to ``person`` and
    ``4``. For a variant carrying a SINGLE placeholder this costs nothing —
    ``websearch_to_tsquery`` already compounds the underscore-joined token into
    ``'person' <-> '4'`` on its own, quoted or not, under both ``english`` and
    ``simple``.

    It is a variant carrying TWO placeholders that needs this. Bare, they fuse
    into one long chain — ``[PERSON_4],[PERSON_1]`` becomes
    ``'person' <-> '4' <-> 'person' <-> '1'``, which demands the two tokens sit
    literally side by side and matches almost nothing. Quoted, each becomes its
    own phrase and they are ANDed: ``'person' <-> '4' & 'person' <-> '1'``.

    This does NOT by itself stop a different person's document from matching —
    that is the ``@@`` matcher's job at the call sites, because ``ts_rank``
    scores loose lexeme overlap and ignores adjacency entirely.

    FTS call sites ONLY. The ``icontains`` arms match whole substrings against
    stored text that contains no quotes, so quoting there would match nothing.
    """
    return _PLACEHOLDER_TOKEN_RE.sub(r'"\g<0>"', variant)


def _active_bindings(tenant) -> dict[str, Any]:
    """Tenant bindings minus the tombstoned ones. Mirrors ``egress._active_entity_map``."""
    entity_map = getattr(tenant, "pii_entity_map", None) or {}
    return {
        placeholder: entry
        for placeholder, entry in entity_map.items()
        if not (isinstance(entry, dict) and entry.get("retired"))
    }


def rehydrate_active(tenant, text: str) -> str:
    """Rehydrate ACTIVE bindings only — retired ones stay as literal tokens.

    Used where the owner's rendered text can come straight back as a write (the
    memory editor): a retired binding that rehydrated to its name would be
    re-authored on save, and since retirement removes the value from
    substitution, the name would land RAW at rest — an owner deleting an entity
    would be the one thing that puts it back in plaintext. Leaving the token in
    place is the honest render: the owner deleted that entity, and the token
    still resolves everywhere it is legitimately rehydrated (directive §A9).
    """
    from apps.pii.redactor import rehydrate_text

    active = _active_bindings(tenant)
    if not text or not active:
        return text
    return rehydrate_text(text, active)


def document_fts_search(base_queryset, variants: list[str], *, limit: int) -> list:
    """Run the Document full-text search: strict match first, recall floor second.

    ONE implementation for both call sites (the agent's ``nbhd_journal_search``
    and the grounding probe that exists to predict it) — a probe that ranked
    differently from the tool it models would report grounding the agent does
    not have.

    Two passes, because the two failure modes pull in opposite directions:

    1. **ALL terms** (``websearch``, which ANDs). This is the real matcher.
       ``ts_rank`` is a similarity score that counts lexeme overlap and ignores
       phrase adjacency — measured at 0.09 for a document Postgres reports as
       non-matching — so ranking alone lets a placeholder variant surface a
       DIFFERENT person's note (``[PERSON_4]`` contributes the loose lexemes
       ``person`` and ``4``).
    2. **ANY term**, and only when pass 1 found NOTHING. Measured: "kitchen
       renovation permits" requires all three, so a note carrying two of them
       matches nothing at all. Answering the agent with an empty result set
       where pre-P3 gave a partial answer is a regression, so the floor widens
       to an OR of the individual terms.

    The floor is deliberately NOT the literal pre-P3 predicate. ``rank > 0``
    reads like "scored something" but is not a predicate at all: measured,
    ``ts_rank`` returns ``1e-20`` — not zero — for a query with no matching
    lexeme whatsoever, so that filter passed EVERY row and a genuinely
    unmatched query dumped the corpus. The OR pass expresses the intended
    best-effort recall with semantics that actually hold.

    Ordering stays keyed to the full-intent query in both passes, so a document
    matching two terms of three still outranks one matching a single term.
    """
    from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

    search_vector = SearchVector("title", weight="A") + SearchVector("markdown", weight="B")
    search_query = SearchQuery(as_fts_phrase(variants[0]), search_type="websearch")
    for variant in variants[1:]:
        search_query = search_query | SearchQuery(as_fts_phrase(variant), search_type="websearch")

    annotated = base_queryset.annotate(
        search=search_vector,
        rank=SearchRank(search_vector, search_query),
    )
    strict = list(annotated.filter(search=search_query).order_by("-rank")[:limit])
    if strict:
        return strict

    any_term_query = None
    for variant in variants:
        for term in variant.split():
            term_query = SearchQuery(as_fts_phrase(term), search_type="websearch")
            any_term_query = term_query if any_term_query is None else (any_term_query | term_query)
    if any_term_query is None:
        return []
    return list(annotated.filter(search=any_term_query).order_by("-rank")[:limit])


def search_query_variants(tenant, query: str) -> list[str]:
    """Original query plus its bounded name→placeholder variants (A5).

    Shares the Task/Goal implementation rather than re-deriving the bounded
    multimap walk — two search surfaces that disagree about which names are
    substitutable is a recall bug nobody would notice until a name went
    missing. Imported lazily: ``lifecycle_views`` imports ``document_views``,
    which imports this module.
    """
    from apps.journal.lifecycle_views import _search_variants

    return _search_variants(tenant, query)
