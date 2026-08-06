"""Server-side PDF text extraction for app-uploaded documents (Phase 1).

Why this exists: the OpenClaw ``pdf`` tool reads a document INSIDE the agent's
turn. The user watches a spinner for the whole read and every queued message
waits behind it (the container runs one turn at a time). This module moves the
common case — a PDF that already carries a text layer — off the turn and into a
QStash background task (``apps.router.tasks.extract_inbound_document_task``), so
the ingress turn acknowledges receipt immediately and the text arrives as a
follow-up turn.

Phase 1 is text-layer PDFs ONLY. A scanned / image-only PDF has no text to pull,
so it falls back to the in-turn ``pdf`` tool (the Gemma vision path pinned by
``agents.defaults.pdfModel``). Server-side vision OCR is Phase 2 and deliberately
not built here. See ``CONTINUITY_async_pdf_extraction.md``.

Extraction state lives entirely on the tenant's file share — no new DB tables,
no migration:

  - ``<doc>.pdf.extracted.txt``  the extracted text (success artifact)
  - ``<doc>.pdf.delivered``      the done-marker. Its presence means the result
    was already handed to the agent, so a QStash redelivery is a no-op rather
    than a second unexplained message to the user.

Both are written through ``upload_workspace_file`` (``text=``), i.e. the
``_put_share_file`` sanitize chokepoint — invariant 2. Never hand-roll a share
upload here.
"""

from __future__ import annotations

import io
import logging

from apps.router.inbound_media import _UNTRUSTED_CONTENT_NOTICE, container_media_path

logger = logging.getLogger(__name__)

# Suffixes appended to the stored document's workspace path. Keeping the full
# original filename (extension included) as the stem means the extraction
# artifacts sort next to their source in the inbound directory and are swept by
# the same directory-wide 24h GC (``cleanup_inbound_media_task``).
EXTRACTED_TEXT_SUFFIX = ".extracted.txt"
DELIVERY_MARKER_SUFFIX = ".delivered"

# Cap on the text written to the share. A 2,000-page document (the ingress
# structural ceiling) can carry millions of characters; the share file is a
# convenience artifact, not an archive, so it is clamped well below that.
MAX_EXTRACTED_CHARS = 400_000

# Cap on the text carried INLINE in the delivery turn. The agent cannot be
# relied on to read an arbitrary share file mid-chat (docs/agents/invariants.md
# §16: the chat-context tool policy strips fs ``read``), so the extracted text
# rides the turn itself and the share path is supplementary. 20k chars ≈ 5k
# tokens — enough for a typical invoice/statement/report, bounded enough that a
# long document can't blow out the turn.
MAX_INLINE_CHARS = 20_000

# Below this many non-whitespace characters we treat the PDF as having no
# usable text layer (scanned / image-only) and hand it back to the in-turn
# ``pdf`` tool. A few stray glyphs from a scanner's header are not a text layer.
MIN_TEXT_LAYER_CHARS = 16


def extracted_text_path(workspace_path: str) -> str:
    """Share-relative path of the extracted-text artifact for ``workspace_path``."""
    return f"{workspace_path}{EXTRACTED_TEXT_SUFFIX}"


def delivery_marker_path(workspace_path: str) -> str:
    """Share-relative path of the done-marker for ``workspace_path``.

    Presence == "the extraction result has already been delivered to the agent".
    Checked before every delivery so a QStash redelivery cannot produce a second
    follow-up message for the same document.
    """
    return f"{workspace_path}{DELIVERY_MARKER_SUFFIX}"


def extraction_dedup_id(tenant_id: str, workspace_path: str) -> str:
    """QStash ``Upstash-Deduplication-Id`` for one document's extraction.

    QStash rejects ``:`` and whitespace in a deduplication id (invariant 6 —
    ``publish_task`` validates eagerly), so this is built from characters QStash
    accepts: the tenant UUID (hyphens only) plus the stored filename, whose
    ``doc_<sha256-prefix>.pdf`` shape is already content-addressed and therefore
    stable across a re-upload of the same bytes. Directory separators are folded
    to ``-`` rather than dropped so two files can never collide on a shared
    basename.
    """
    stem = workspace_path.replace("/", "-")
    return f"pdfextract-{tenant_id}-{stem}".replace(":", "-").replace(" ", "-")


def extract_pdf_text(data: bytes) -> str:
    """Return the text layer of ``data``, or ``""`` when there is none.

    Pure-Python (pypdf) — no native toolchain, no OCR. A per-page failure is
    logged and skipped rather than aborting the whole document: a partially
    damaged PDF still yields the pages that do parse, which is strictly better
    for the user than a hard failure. Accumulation stops once
    ``MAX_EXTRACTED_CHARS`` is reached so a pathological document can't force an
    unbounded allocation.

    Encrypted PDFs never reach here — ``inbound_media._scan_pdf_structure``
    rejects an ``/Encrypt`` trailer at ingress.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)

    chunks: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            # No raw content in the log line — only the page index (mirrors the
            # no-raw-value discipline in ``_store_inbound_media``).
            logger.warning("pdf_extract: page %d failed to parse — skipping", index, exc_info=True)
            continue
        if not page_text.strip():
            continue
        chunks.append(page_text)
        total += len(page_text)
        if total >= MAX_EXTRACTED_CHARS:
            break

    text = "\n\n".join(chunks)[:MAX_EXTRACTED_CHARS]
    if len(text.split()) and len("".join(text.split())) >= MIN_TEXT_LAYER_CHARS:
        return text
    return ""


def redact_extracted_document_text(tenant, text: str) -> str:
    """THE PII SEAM for extracted document text — currently a pass-through.

    Every character of extracted text reaches the model through this one
    function, so wiring redaction in later is a single-call-site change rather
    than an audit of the extraction path.

    Deliberately NOT wired in this build: the document/tool-response redaction
    chokepoint is an open audit item (``docs/pii-coverage-audit-2026-08-04.md``
    — ``redact_tool_response`` exists but is unwired), and turning it on here
    ahead of that decision would redact document text on a different policy than
    the in-turn ``pdf`` tool applies to the SAME document. Leaving one named
    seam keeps the two paths convergeable.

    ``tenant`` is accepted (and ignored) now so the signature does not change
    when the redactor — which is per-tenant, entity-map bound — is wired in.
    """
    return text


def _clamp_for_turn(text: str, extracted_container_path: str) -> str:
    """Clamp ``text`` to the inline turn budget, naming where the full copy lives."""
    if len(text) <= MAX_INLINE_CHARS:
        return text
    return (
        text[:MAX_INLINE_CHARS] + f"\n\n[... truncated at {MAX_INLINE_CHARS} characters. "
        f"The complete extracted text is on the share at {extracted_container_path}.]"
    )


def build_extraction_ready_turn(*, workspace_path: str, text: str) -> str:
    """The system turn delivered to the agent when extraction succeeded.

    The extracted text rides INLINE (clamped) rather than only as a path: the
    chat-context tool policy strips fs ``read`` (invariant 16), so an agent told
    only "the text is at <path>" has no way to open it mid-conversation. The
    share path is still named — it is the durable artifact and the fallback for
    a truncated read.

    Carries the same untrusted-content framing as the ingress attachment marker
    (``inbound_media._UNTRUSTED_CONTENT_NOTICE``), byte-identical on purpose: the
    extracted text is the same third-party bytes the marker was written to
    defend against, just delivered a turn later.
    """
    extracted_container = container_media_path(extracted_text_path(workspace_path))
    filename = workspace_path.rsplit("/", 1)[-1]
    body = _clamp_for_turn(text, extracted_container)
    return (
        f"[Document extraction ready: {filename} → text saved at {extracted_container} "
        f"({len(text):,} chars). The full text follows below — do NOT call the pdf tool "
        f"for this file. Respond to the user's original request now, replying in THIS "
        f"turn rather than sending a separate message. {_UNTRUSTED_CONTENT_NOTICE}]\n"
        "\n--- BEGIN EXTRACTED DOCUMENT TEXT ---\n"
        f"{body}\n"
        "--- END EXTRACTED DOCUMENT TEXT ---\n"
    )


def build_extraction_fallback_turn(*, workspace_path: str, reason: str) -> str:
    """The system turn delivered when the async path cannot produce text.

    Two cases, one shape: a scanned/image-only PDF (no text layer — Phase 1 does
    not OCR) and a genuine extraction failure. Both hand the document back to the
    in-turn ``pdf`` tool, which still works; the ingress marker told the agent not
    to call it "unless extraction fails", and this turn is that signal.

    Failure honesty (directive): the user is never left waiting on a background
    job that quietly died — the agent is always told what to do next.
    """
    container_path = container_media_path(workspace_path)
    filename = workspace_path.rsplit("/", 1)[-1]
    return (
        f"[Document extraction unavailable for {filename} ({reason}). "
        f"Read it now with your pdf tool at {container_path}, then respond to the "
        f"user's original request, replying in THIS turn rather than sending a "
        f"separate message. {_UNTRUSTED_CONTENT_NOTICE}]\n"
    )
