"""QStash-callable task functions for the lessons app."""
from __future__ import annotations

import logging
from datetime import date

from django.utils import timezone

logger = logging.getLogger(__name__)


def reseed_lessons_task() -> dict:
    """Fan out reseed to each active tenant via QStash."""
    from apps.tenants.models import Tenant

    tenants = list(Tenant.objects.filter(status=Tenant.Status.ACTIVE))
    logger.info("reseed_lessons: enqueueing %d tenants", len(tenants))

    from apps.cron.publish import publish_task

    for tenant in tenants:
        publish_task("reseed_lessons_single_tenant", tenant_id=str(tenant.id))

    return {"ok": True, "tenants_enqueued": len(tenants)}


def reseed_lessons_single_tenant_task(tenant_id: str) -> dict:
    """Delete journal-sourced lessons and re-extract for a single tenant."""
    from apps.journal.extraction import (
        MIN_NOTE_LENGTH,
        _call_extraction_llm,
        _embedding_duplicate,
    )
    from apps.journal.models import DailyNote, Document, PendingExtraction
    from apps.lessons.clustering import refresh_constellation
    from apps.lessons.models import Lesson
    from apps.lessons.services import process_approved_lesson
    from apps.tenants.models import Tenant

    tenant = Tenant.objects.get(id=tenant_id)
    tid = str(tenant.id)[:8]

    # ── Delete existing journal-sourced lessons ──
    deleted_lessons, _ = Lesson.objects.filter(
        tenant=tenant, source_type="journal",
    ).delete()
    deleted_pending, _ = PendingExtraction.objects.filter(
        tenant=tenant, kind=PendingExtraction.Kind.LESSON,
    ).delete()
    logger.info("reseed[%s]: cleared %d lessons, %d pending", tid, deleted_lessons, deleted_pending)

    # ── Gather daily notes ──
    notes = _gather_notes(tenant)
    logger.info("reseed[%s]: found %d daily notes", tid, len(notes))
    if not notes:
        return {"tenant": tid, "notes": 0, "added": 0}

    # ── Batch and extract ──
    batches = _batch_notes(notes)
    logger.info("reseed[%s]: %d batches", tid, len(batches))

    total_extracted = 0
    total_deduped = 0
    total_added = 0

    for i, batch_content in enumerate(batches, 1):
        try:
            extracted, _usage = _call_extraction_llm(batch_content)
        except Exception as e:
            logger.error("reseed[%s]: batch %d LLM error — %s", tid, i, e)
            continue

        lessons_raw = extracted.get("lessons", [])
        total_extracted += len(lessons_raw)

        for item in lessons_raw:
            text = (item.get("text") or "").strip()
            if not text or len(text) < 20:
                continue

            if _embedding_duplicate(tenant, text):
                total_deduped += 1
                continue

            lesson = Lesson.objects.create(
                tenant=tenant,
                text=text,
                context=f"Re-seeded from daily notes — {date.today().isoformat()}",
                tags=item.get("tags", []),
                source_type="journal",
                source_ref="reseed",
                status="approved",
                approved_at=timezone.now(),
            )
            try:
                process_approved_lesson(lesson)
            except Exception as e:
                logger.warning("reseed[%s]: embedding failed for lesson %s: %s", tid, lesson.id, e)

            total_added += 1

    logger.info(
        "reseed[%s]: extracted=%d deduped=%d added=%d",
        tid, total_extracted, total_deduped, total_added,
    )

    # ── Re-cluster ──
    if total_added > 0:
        try:
            result = refresh_constellation(tenant)
            logger.info("reseed[%s]: re-clustered: %s", tid, result)
        except Exception as e:
            logger.error("reseed[%s]: clustering failed: %s", tid, e)

    return {
        "tenant": tid,
        "notes": len(notes),
        "extracted": total_extracted,
        "deduped": total_deduped,
        "added": total_added,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

BATCH_CHAR_LIMIT = 5500


def _gather_notes(tenant) -> list[tuple[date, str]]:
    """Return (date, markdown) pairs from both v2 Documents and v1 DailyNotes."""
    from apps.journal.extraction import MIN_NOTE_LENGTH
    from apps.journal.models import DailyNote, Document

    seen_dates: set[date] = set()
    notes: list[tuple[date, str]] = []

    for doc in Document.objects.filter(tenant=tenant, kind=Document.Kind.DAILY).order_by("slug"):
        try:
            d = date.fromisoformat(doc.slug)
        except ValueError:
            continue
        if len(doc.markdown) >= MIN_NOTE_LENGTH:
            notes.append((d, doc.markdown))
            seen_dates.add(d)

    for dn in DailyNote.objects.filter(tenant=tenant).order_by("date"):
        if dn.date in seen_dates:
            continue
        if len(dn.markdown) >= MIN_NOTE_LENGTH:
            notes.append((dn.date, dn.markdown))

    notes.sort(key=lambda x: x[0])
    return notes


def _batch_notes(notes: list[tuple[date, str]]) -> list[str]:
    """Group notes into batches that fit within the LLM char limit."""
    batches: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for d, markdown in notes:
        entry = f"## {d}\n{markdown}"
        entry_len = len(entry)

        if current_len + entry_len > BATCH_CHAR_LIMIT and current_parts:
            batches.append("\n\n".join(current_parts))
            current_parts = []
            current_len = 0

        current_parts.append(entry)
        current_len += entry_len

    if current_parts:
        batches.append("\n\n".join(current_parts))

    return batches
