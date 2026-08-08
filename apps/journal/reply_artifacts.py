"""Journal-owned persistence helpers for assistant reply artifacts."""

from __future__ import annotations

import hashlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.integrations.content_sanitize import neutralize_remote_image_markdown
from apps.journal.models import Document

_MAX_SLUG_ATTEMPTS = 4
_MARKER_PREFIX = "<!-- nbhd-reply-artifact:v1:"


class ReplyArtifactCollisionError(RuntimeError):
    """All deterministic artifact slugs were occupied by unrelated documents."""


def _tenant_now(tenant):
    tz_name = getattr(getattr(tenant, "user", None), "timezone", None) or "UTC"
    try:
        return timezone.now().astimezone(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        return timezone.now().astimezone(ZoneInfo("UTC"))


def _identity(*, tenant, source: str, dedup_key: str) -> str:
    return hashlib.sha256(f"{tenant.id}|{source}|{dedup_key}".encode()).hexdigest()


def _marker(*, source: str, identity: str) -> str:
    return f"{_MARKER_PREFIX}{source}:{identity} -->"


def _slug(*, local_date, identity: str, attempt: int) -> str:
    digest = identity
    if attempt:
        digest = hashlib.sha256(f"{identity}|collision|{attempt}".encode()).hexdigest()
    return f"assistant-table-{local_date:%Y%m%d}-{digest[:24]}"


def upsert_reply_artifact(*, tenant, source: str, dedup_key: str, title: str, markdown: str) -> Document:
    """Create or update the one Journal document for a persisted reply.

    ``Document.Kind.PROJECT`` is the v1 compatibility choice. A dedicated
    artifact kind is the intended follow-up once every Journal client and
    runtime route understands it.
    """
    if source not in {"ios", "proactive"}:
        raise ValueError("source must be 'ios' or 'proactive'")
    if not dedup_key:
        raise ValueError("dedup_key is required")

    from apps.pii.authoring import author_text

    identity = _identity(tenant=tenant, source=source, dedup_key=dedup_key)
    marker = _marker(source=source, identity=identity)
    clean_markdown = neutralize_remote_image_markdown(markdown)
    # Author the BODY only, never the marker: the marker is this artifact's
    # identity (``startswith`` decides update-vs-collision below) and running a
    # redactor over it could rewrite the very token the next call matches on.
    authored_markdown = author_text(
        tenant,
        clean_markdown,
        seam="journal.reply_artifact.upsert",
        writer="background",
        field="markdown",
        model_label="journal.Document",
    )
    stored_markdown = f"{marker}\n{authored_markdown.text}"
    authored_title = author_text(
        tenant,
        title.strip()[:80],
        seam="journal.reply_artifact.upsert",
        writer="background",
        field="title",
        model_label="journal.Document",
    )
    clean_title = authored_title.text
    receipts = {"title": authored_title.receipt, "markdown": authored_markdown.receipt}

    local_date = _tenant_now(tenant).date()
    for attempt in range(_MAX_SLUG_ATTEMPTS):
        slug = _slug(local_date=local_date, identity=identity, attempt=attempt)
        candidate = Document.objects.filter(
            tenant=tenant,
            kind=Document.Kind.PROJECT,
            slug=slug,
        ).first()
        if candidate is not None:
            if candidate.markdown.startswith(marker):
                candidate.title = clean_title
                candidate.markdown = stored_markdown
                candidate.pii_receipts = {**(candidate.pii_receipts or {}), **receipts}
                candidate.save(update_fields=["title", "markdown", "pii_receipts", "updated_at"])
                return candidate
            continue

        try:
            with transaction.atomic():
                return Document.objects.create(
                    tenant=tenant,
                    kind=Document.Kind.PROJECT,
                    slug=slug,
                    title=clean_title,
                    markdown=stored_markdown,
                    pii_receipts=receipts,
                )
        except IntegrityError:
            candidate = Document.objects.filter(
                tenant=tenant,
                kind=Document.Kind.PROJECT,
                slug=slug,
            ).first()
            if candidate is not None and candidate.markdown.startswith(marker):
                return candidate

    raise ReplyArtifactCollisionError("all deterministic reply artifact slugs are occupied")


def document_contains_tables(doc: Document, tables) -> bool:
    """Return whether ``doc`` contains every selected table.

    The stored body is placeholder-space, so a table whose cells name a bound
    entity is NOT there verbatim — comparing raw would read as a chip collision
    and fork a duplicate artifact document. The table text is put through the
    same deterministic known-value substitution the stored body went through
    before comparing.

    ``redact_known_values`` and not ``author_text`` on purpose: this is a
    read-only predicate, and the authoring chokepoint can MINT new bindings.
    Residual: a structured entity that authoring minted for the first time (an
    email address in a cell) has no binding to substitute here, so that one
    table still misses and forks a duplicate. Bounded and self-healing — the
    binding exists from the next turn on.
    """
    from apps.pii.egress import redact_known_values

    markdown = doc.markdown or ""
    tenant = doc.tenant
    return all(
        redact_known_values(
            tenant,
            neutralize_remote_image_markdown(table.text).strip(),
            seam="journal.reply_artifact.contains",
        )
        in markdown
        for table in tables
    )


def artifact_journal_link(doc: Document) -> dict:
    """Build the placeholder-space chat chip coordinate for ``doc``."""
    return {"kind": doc.kind, "slug": doc.slug, "title": doc.title[:80]}
