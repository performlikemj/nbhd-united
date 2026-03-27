"""One-time re-seed of constellation lessons from all daily notes.

Deletes existing journal-sourced lessons, then sweeps each active tenant's
full daily note history through the (improved) extraction prompt with
embedding-based deduplication.  Intended to be run once after fixing the
extraction prompt framing and dedup logic.
"""
from __future__ import annotations

import time
from datetime import date

from django.core.management.base import BaseCommand
from django.utils import timezone

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

# Stay under the 6000-char truncation in _call_extraction_llm
BATCH_CHAR_LIMIT = 5500


class Command(BaseCommand):
    help = "Delete journal-sourced lessons and re-extract from all daily notes"

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=str, help="Only process this tenant ID")
        parser.add_argument("--dry-run", action="store_true", help="Show extractions without saving")
        parser.add_argument("--since", type=str, help="Only process notes from this date (YYYY-MM-DD)")

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        since = None
        if options.get("since"):
            since = date.fromisoformat(options["since"])

        qs = Tenant.objects.filter(status=Tenant.Status.ACTIVE)
        if options.get("tenant"):
            qs = qs.filter(id=options["tenant"])

        tenants = list(qs)
        self.stdout.write(f"Processing {len(tenants)} tenant(s), dry_run={dry_run}")

        for tenant in tenants:
            self._process_tenant(tenant, dry_run=dry_run, since=since)

    def _process_tenant(self, tenant: Tenant, *, dry_run: bool, since: date | None) -> None:
        tid = str(tenant.id)[:8]
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"Tenant {tid}")

        # ── Delete existing journal-sourced lessons ──
        if not dry_run:
            deleted_lessons, _ = Lesson.objects.filter(
                tenant=tenant, source_type="journal",
            ).delete()
            deleted_pending, _ = PendingExtraction.objects.filter(
                tenant=tenant, kind=PendingExtraction.Kind.LESSON,
            ).delete()
            self.stdout.write(f"  Cleared {deleted_lessons} lessons, {deleted_pending} pending extractions")
        else:
            existing = Lesson.objects.filter(tenant=tenant, source_type="journal").count()
            self.stdout.write(f"  Would delete {existing} existing journal lessons")

        # ── Gather daily notes ──
        notes = self._gather_notes(tenant, since)
        self.stdout.write(f"  Found {len(notes)} daily notes")
        if not notes:
            return

        # ── Batch and extract ──
        batches = self._batch_notes(notes)
        self.stdout.write(f"  Grouped into {len(batches)} batches")

        total_extracted = 0
        total_deduped = 0
        total_added = 0

        for i, batch_content in enumerate(batches, 1):
            try:
                extracted, _usage = _call_extraction_llm(batch_content)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Batch {i}: LLM error — {e}"))
                continue

            lessons_raw = extracted.get("lessons", [])
            total_extracted += len(lessons_raw)

            for item in lessons_raw:
                text = (item.get("text") or "").strip()
                if not text or len(text) < 20:
                    continue

                # Semantic dedup against already-seeded lessons in this run
                if not dry_run and _embedding_duplicate(tenant, text):
                    total_deduped += 1
                    continue

                if dry_run:
                    self.stdout.write(f"    [batch {i}] {text[:100]}")
                    total_added += 1
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
                    self.stdout.write(self.style.WARNING(f"    Embedding failed for lesson {lesson.id}: {e}"))

                total_added += 1

            # Rate limit between batches
            time.sleep(1)

        self.stdout.write(
            f"  Result: {total_extracted} extracted, {total_deduped} deduped, {total_added} added"
        )

        # ── Re-cluster ──
        if not dry_run and total_added > 0:
            try:
                result = refresh_constellation(tenant)
                self.stdout.write(f"  Re-clustered: {result}")
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Clustering failed: {e}"))

    def _gather_notes(self, tenant: Tenant, since: date | None) -> list[tuple[date, str]]:
        """Return (date, markdown) pairs from both v2 Documents and v1 DailyNotes."""
        seen_dates: set[date] = set()
        notes: list[tuple[date, str]] = []

        # v2 Documents first (preferred)
        doc_qs = Document.objects.filter(tenant=tenant, kind=Document.Kind.DAILY)
        if since:
            doc_qs = doc_qs.filter(slug__gte=str(since))
        for doc in doc_qs.order_by("slug"):
            try:
                d = date.fromisoformat(doc.slug)
            except ValueError:
                continue
            if len(doc.markdown) >= MIN_NOTE_LENGTH:
                notes.append((d, doc.markdown))
                seen_dates.add(d)

        # v1 DailyNotes as fallback for dates not covered by v2
        dn_qs = DailyNote.objects.filter(tenant=tenant)
        if since:
            dn_qs = dn_qs.filter(date__gte=since)
        for dn in dn_qs.order_by("date"):
            if dn.date in seen_dates:
                continue
            if len(dn.markdown) >= MIN_NOTE_LENGTH:
                notes.append((dn.date, dn.markdown))

        notes.sort(key=lambda x: x[0])
        return notes

    def _batch_notes(self, notes: list[tuple[date, str]]) -> list[str]:
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
