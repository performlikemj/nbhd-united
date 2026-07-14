"""AAD coordinates + ladder groups for the encrypted journal columns.

Encryption-at-rest Phase 3 (plan §1.1, §3). Every producer (dual-write),
consumer (dual-read), the ``encrypt_journal_history`` backfill, and the
completeness predicate import these — the AAD ``(table, column)`` strings are
byte-identical at every site forever (a drift makes prior rows undecryptable;
directive red-team #1). NEVER hand-type the strings.

Each AAD ``column`` is the *logical* plaintext column name (``title``,
``markdown``), NOT the ``_enc`` sidecar that stores the envelope — so a future
contract migration that drops the plaintext column never has to re-key.

Flag pair: this group flips under ``Tenant.encrypt_journal_writes`` /
``read_encrypted_journal`` (plan §3.1), SHARED with lessons + insights + core
(they co-feed the USER.md envelope / ``memory_sync`` and read as one memory
surface). The completeness predicate enumerates this app's ``JOURNAL_ENC_COLUMNS``
alongside ``apps.lessons.enc_columns.LESSONS_ENC_COLUMNS`` etc. under that flag.

EXCLUDED from Phase 3 by design (do NOT add here without a plan update):
  * ``Document.markdown`` / ``Document.title`` — the crown-jewel, search-coupled
    columns; deferred to Phase 3b with the blind index (MJ Option A, plan §2).
  * ``UserMemory.markdown`` / ``JournalEntry.*`` / ``WeeklyReview.*`` — legacy v1
    stores, DEFER/verify pending a live-writer + row-count check (plan §1.1).
  * ``Document.target`` / ``NoteTemplate.*`` / ``Session.references`` /
    ``DocumentIngestion.source_ref`` — structured/id/system metadata, OUT.
"""

from __future__ import annotations

from apps.crypto.enc_columns import EncColumn

# ── AAD 2-tuples (table, logical column) — box.encrypt(tid, *AAD, value) ──────
# journal_pending_extractions
PENDING_EXTRACTION_TEXT: tuple[str, str] = ("journal_pending_extractions", "text")
# journal_goals
GOAL_TITLE: tuple[str, str] = ("journal_goals", "title")
GOAL_DESCRIPTION: tuple[str, str] = ("journal_goals", "description")
# journal_purposes
PURPOSE_STATEMENT: tuple[str, str] = ("journal_purposes", "statement")
PURPOSE_EVIDENCE: tuple[str, str] = ("journal_purposes", "evidence")
# journal_tasks
TASK_TITLE: tuple[str, str] = ("journal_tasks", "title")
TASK_DESCRIPTION: tuple[str, str] = ("journal_tasks", "description")
# journal_pending_task_actions
PENDING_TASK_ACTION_EVIDENCE: tuple[str, str] = ("journal_pending_task_actions", "evidence")
# journal_document_chunks
DOCUMENT_CHUNK_TEXT: tuple[str, str] = ("journal_document_chunks", "text")
# journal_document_ingestions
DOCUMENT_INGESTION_ORIGINAL_FILENAME: tuple[str, str] = ("journal_document_ingestions", "original_filename")
# journal_document_ingestion_artifacts
DOCUMENT_INGESTION_ARTIFACT_CONTENT_EXCERPT: tuple[str, str] = (
    "journal_document_ingestion_artifacts",
    "content_excerpt",
)
# journal_dailynote
DAILY_NOTE_MARKDOWN: tuple[str, str] = ("journal_dailynote", "markdown")
# journal_sessions
SESSION_SUMMARY: tuple[str, str] = ("journal_sessions", "summary")
SESSION_ACCOMPLISHMENTS: tuple[str, str] = ("journal_sessions", "accomplishments")
SESSION_BLOCKERS: tuple[str, str] = ("journal_sessions", "blockers")
SESSION_NEXT_STEPS: tuple[str, str] = ("journal_sessions", "next_steps")

# ── Ladder group — (model, value_field, enc_field) consumed by writers/backfill/
# read-helper/completeness predicate. ``table`` verified against each model's
# Meta.db_table. NOTE: every model here carries a direct ``tenant`` FK, so the
# predicate filters ``<model>.objects.filter(tenant=..., <enc>__isnull=True)``.
JOURNAL_ENC_COLUMNS: tuple[EncColumn, ...] = (
    EncColumn("journal.PendingExtraction", "text", "text_enc", "journal_pending_extractions"),
    EncColumn("journal.Goal", "title", "title_enc", "journal_goals"),
    EncColumn("journal.Goal", "description", "description_enc", "journal_goals"),
    EncColumn("journal.Purpose", "statement", "statement_enc", "journal_purposes"),
    EncColumn("journal.Purpose", "evidence", "evidence_enc", "journal_purposes", is_json=True),
    EncColumn("journal.Task", "title", "title_enc", "journal_tasks"),
    EncColumn("journal.Task", "description", "description_enc", "journal_tasks"),
    EncColumn("journal.PendingTaskAction", "evidence", "evidence_enc", "journal_pending_task_actions"),
    EncColumn("journal.DocumentChunk", "text", "text_enc", "journal_document_chunks"),
    EncColumn("journal.DocumentIngestion", "original_filename", "original_filename_enc", "journal_document_ingestions"),
    EncColumn(
        "journal.DocumentIngestionArtifact",
        "content_excerpt",
        "content_excerpt_enc",
        "journal_document_ingestion_artifacts",
    ),
    EncColumn("journal.DailyNote", "markdown", "markdown_enc", "journal_dailynote"),
    EncColumn("journal.Session", "summary", "summary_enc", "journal_sessions"),
    EncColumn("journal.Session", "accomplishments", "accomplishments_enc", "journal_sessions", is_json=True),
    EncColumn("journal.Session", "blockers", "blockers_enc", "journal_sessions", is_json=True),
    EncColumn("journal.Session", "next_steps", "next_steps_enc", "journal_sessions", is_json=True),
)
