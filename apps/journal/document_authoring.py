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
    writer: str,
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
        writer=writer,
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
