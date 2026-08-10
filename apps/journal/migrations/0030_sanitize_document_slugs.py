"""Make every stored journal document slug console-addressable.

The runtime write API historically accepted dots and underscores while the
console read API rejected them before querying the database. Rename every row
outside the console's slug charset so sidebar entries cannot point at a
guaranteed 404.

Like ``0017_cleanup_garbage_journal_documents``, this fleet-wide data migration
uses the historical model on Django's migration connection. Production
migrations run as the ``postgres`` BYPASSRLS role documented in
``tenants.0059_lock_down_public_schema_rls``, so no request-scoped tenant GUC is
needed or set here.
"""

from __future__ import annotations

import logging
import re

from django.db import migrations

logger = logging.getLogger(__name__)

_CONSOLE_SLUG_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9\-/]*$"
_INVALID_CHARACTER_RE = re.compile(r"[^a-zA-Z0-9/-]")
_LEADING_NON_ALPHANUMERIC_RE = re.compile(r"^[^a-zA-Z0-9]+")
_MAX_SLUG_LENGTH = 128


def _base_slug(slug, document_id):
    sanitized = _INVALID_CHARACTER_RE.sub("-", slug)
    sanitized = _LEADING_NON_ALPHANUMERIC_RE.sub("", sanitized)
    if not sanitized:
        sanitized = f"doc-{document_id.hex[:8]}"
    return sanitized[:_MAX_SLUG_LENGTH]


def _available_slug(Document, document, base):
    candidate = base
    suffix_number = 2
    collisions = Document.objects.filter(
        tenant_id=document.tenant_id,
        kind=document.kind,
        slug=candidate,
    ).exclude(pk=document.pk)
    while collisions.exists():
        suffix = f"-{suffix_number}"
        candidate = f"{base[: _MAX_SLUG_LENGTH - len(suffix)]}{suffix}"
        suffix_number += 1
        collisions = Document.objects.filter(
            tenant_id=document.tenant_id,
            kind=document.kind,
            slug=candidate,
        ).exclude(pk=document.pk)
    return candidate


def sanitize_document_slugs(apps, schema_editor):
    Document = apps.get_model("journal", "Document")
    invalid_documents = (
        Document.objects.exclude(slug__regex=_CONSOLE_SLUG_PATTERN)
        .only("id", "tenant_id", "kind", "slug")
        .order_by("id")
    )

    renamed = 0
    for document in invalid_documents.iterator():
        base = _base_slug(document.slug, document.pk)
        new_slug = _available_slug(Document, document, base)
        # QuerySet.update intentionally bypasses auto_now on updated_at. A slug
        # repair must not make an old document look newly touched in recency UI.
        renamed += Document.objects.filter(pk=document.pk).update(slug=new_slug)

    logger.info("renamed %d journal document slugs", renamed)


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0029_notetemplate_pii_receipts_session_pii_receipts"),
    ]

    operations = [
        migrations.RunPython(sanitize_document_slugs, migrations.RunPython.noop),
    ]
