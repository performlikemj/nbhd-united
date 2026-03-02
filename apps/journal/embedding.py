"""Chunk and embed daily notes for contextual recall.

Called nightly after extraction. Splits daily notes by section headings,
embeds each chunk via OpenAI, and stores in DocumentChunk for pgvector search.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from apps.journal.models import Document, DocumentChunk
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Rough token estimate: 1 token ≈ 4 chars for English text
MAX_CHUNK_CHARS = 2000  # ~500 tokens
MIN_CHUNK_CHARS = 80    # skip trivially small chunks


def chunk_markdown(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split markdown into chunks by ## headings, then by paragraphs if too long.

    Returns a list of chunk strings, each ≤ max_chars.
    """
    if not text or not text.strip():
        return []

    # Split on ## headings (keep the heading with its section)
    sections = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    sections = [s.strip() for s in sections if s.strip()]

    chunks: list[str] = []
    for section in sections:
        if len(section) <= max_chars:
            if len(section) >= MIN_CHUNK_CHARS:
                chunks.append(section)
            continue

        # Section too long — split on double newlines (paragraphs)
        paragraphs = re.split(r"\n\n+", section)
        current = ""
        for para in paragraphs:
            if current and len(current) + len(para) + 2 > max_chars:
                if len(current) >= MIN_CHUNK_CHARS:
                    chunks.append(current.strip())
                current = para
            else:
                current = f"{current}\n\n{para}" if current else para

        if current.strip() and len(current.strip()) >= MIN_CHUNK_CHARS:
            chunks.append(current.strip())

    return chunks


def embed_daily_note(tenant: Tenant, for_date: date) -> int:
    """Chunk and embed a tenant's daily note for the given date.

    Deletes existing chunks for the same document before re-creating (idempotent).
    Returns the number of chunks created.
    """
    from apps.lessons.services import generate_embedding

    # Find the daily note document
    doc = Document.objects.filter(
        tenant=tenant, kind=Document.Kind.DAILY, slug=str(for_date)
    ).first()

    if not doc or not doc.markdown.strip():
        logger.info("embed: no daily note for tenant %s date %s", str(tenant.id)[:8], for_date)
        return 0

    # Chunk the markdown
    raw_chunks = chunk_markdown(doc.markdown)
    if not raw_chunks:
        logger.info("embed: no substantial chunks for tenant %s date %s", str(tenant.id)[:8], for_date)
        return 0

    # Prefix each chunk with date for context
    dated_chunks = [f"[{for_date}] {chunk}" for chunk in raw_chunks]

    # Delete old chunks for this document (idempotent re-embedding)
    deleted, _ = DocumentChunk.objects.filter(document=doc).delete()
    if deleted:
        logger.info("embed: deleted %d old chunks for doc %s", deleted, str(doc.id)[:8])

    # Embed and create chunks
    created = 0
    for i, chunk_text in enumerate(dated_chunks):
        try:
            embedding = generate_embedding(chunk_text)
        except Exception:
            logger.exception("embed: failed to embed chunk %d for tenant %s", i, str(tenant.id)[:8])
            continue

        DocumentChunk.objects.create(
            tenant=tenant,
            document=doc,
            chunk_index=i,
            text=chunk_text,
            embedding=embedding,
            source_date=for_date,
        )
        created += 1

    logger.info("embed: created %d chunks for tenant %s date %s", created, str(tenant.id)[:8], for_date)
    return created
