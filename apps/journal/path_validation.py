"""Path-component validation for journal Document kind/slug.

The agent (LLM-driven runtime client) supplies ``kind`` and ``slug`` strings
that flow through the runtime endpoints into ``journal_document`` rows AND
into Azure SMB file share paths shaped like ``memory/journal/<kind>/<slug>.md``
(see ``apps.orchestrator.memory_sync``). Treat both as path components from
an untrusted source: validate at the trust boundary so the file share never
sees NTFS-hostile names, escape sequences, or out-of-enum kinds.

Reused in two places:

1. ``apps.integrations.runtime_views`` — endpoint boundary, hard reject 400.
2. ``apps.orchestrator.memory_sync`` — defense-in-depth, skip-with-warning
   so a future direct DB write that bypasses validation can't grind the
   sync worker against an SMB-hostile path.

History: the canary tenant accumulated two garbage rows (``kind=':' slug=':'``
and ``kind='cron' slug='_sync:Heartbeat Check-in'``) because the runtime
endpoint pre-this-module accepted any string. The ``:`` row produced
``memory/journal/:/:.md`` — NTFS reserves ``:`` (alternate data stream
separator) — and every ``sync_documents_to_workspace`` invocation made ~6
failed SMB roundtrips against it.
"""

from __future__ import annotations

import re

from apps.journal.models import Document

VALID_KINDS: frozenset[str] = frozenset(c.value for c in Document.Kind)

# Mirrors ``apps.journal.document_views._VALID_SLUG_RE`` (user-facing) but
# allows ``.`` for legitimate ISO-date slugs like ``2026-05-15``. The leading
# character must be alphanumeric so a slug can't start with ``/``, ``-``, or
# ``.`` — defense against absolute paths, option flags, and hidden files.
RUNTIME_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")

MAX_SLUG_LEN = 128  # matches Document.slug max_length


def validate_kind_slug(kind: str, slug: str) -> tuple[str, str] | None:
    """Return ``(error_code, detail)`` if invalid, else ``None``.

    Caller (HTTP endpoint) maps the error to a 400 Response; library callers
    (``memory_sync``) treat a non-None return as "skip this row."
    """
    if kind not in VALID_KINDS:
        return (
            "invalid_kind",
            f"kind must be one of: {sorted(VALID_KINDS)}",
        )
    if not slug:
        return ("invalid_slug", f"slug must be 1..{MAX_SLUG_LEN} chars")
    if len(slug) > MAX_SLUG_LEN:
        return ("invalid_slug", f"slug must be 1..{MAX_SLUG_LEN} chars")
    if not RUNTIME_SLUG_RE.match(slug):
        return (
            "invalid_slug",
            f"slug contains invalid characters: {slug!r}",
        )
    if ".." in slug.split("/"):
        return ("invalid_slug", "slug may not contain '..' segments")
    return None
