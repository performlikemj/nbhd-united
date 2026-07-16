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

    identity = _identity(tenant=tenant, source=source, dedup_key=dedup_key)
    marker = _marker(source=source, identity=identity)
    clean_markdown = neutralize_remote_image_markdown(markdown)
    stored_markdown = f"{marker}\n{clean_markdown}"
    clean_title = title.strip()[:80]

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
                candidate.save(update_fields=["title", "markdown", "updated_at"])
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
    """Return whether ``doc`` contains every selected table verbatim."""
    markdown = doc.markdown or ""
    return all(neutralize_remote_image_markdown(table.text).strip() in markdown for table in tables)


def artifact_journal_link(doc: Document) -> dict:
    """Build the placeholder-space chat chip coordinate for ``doc``."""
    return {"kind": doc.kind, "slug": doc.slug, "title": doc.title[:80]}
