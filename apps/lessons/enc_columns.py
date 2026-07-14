"""AAD coordinates + ladder group for the encrypted lessons columns.

Encryption-at-rest Phase 3 (plan §1.2, §3). Same contract as
``apps.journal.enc_columns`` — byte-stable AAD, never hand-typed. Lessons share
the journal flag pair (``Tenant.encrypt_journal_writes`` / ``read_encrypted_journal``,
plan §3.1); the completeness predicate enumerates ``LESSONS_ENC_COLUMNS`` under it.

Search note: ``Lesson.text`` is retrieved via pgvector cosine
(``nbhd_lesson_search``), NOT Postgres FTS — encrypting it touches only the
post-fetch ``.text`` read, no WHERE/ORDER BY (plan §1.2). The embedding stays
plaintext floats (``generate_embedding`` decrypts in-process before the model
call); disclosed residual, not a Phase-3 target.

EXCLUDED: ``Lesson.cluster_label`` — auto cluster name, verdict 3b/assess (plan §1.2).
"""

from __future__ import annotations

from apps.crypto.enc_columns import EncColumn

# ── AAD 2-tuples (table, logical column) ─────────────────────────────────────
# lessons
LESSON_TEXT: tuple[str, str] = ("lessons", "text")
LESSON_GALAXY_NOTE: tuple[str, str] = ("lessons", "galaxy_note")
LESSON_CONTEXT: tuple[str, str] = ("lessons", "context")
# tutoring_sessions
TUTORING_SESSION_MESSAGES: tuple[str, str] = ("tutoring_sessions", "messages")
TUTORING_SESSION_CONNECTIONS_MADE: tuple[str, str] = ("tutoring_sessions", "connections_made")
# star_journal_entries
STAR_JOURNAL_ENTRY_TEXT: tuple[str, str] = ("star_journal_entries", "text")

# ── Ladder group ─────────────────────────────────────────────────────────────
# NOTE: ``TutoringSession`` has NO direct ``tenant`` FK — its tenant is reached
# via ``star.tenant`` (star -> Lesson). The completeness/backfill code must scope
# it with ``.filter(star__tenant=tenant, ...)``, not ``tenant=...``. Lesson and
# StarJournalEntry both carry a direct ``tenant`` FK.
LESSONS_ENC_COLUMNS: tuple[EncColumn, ...] = (
    EncColumn("lessons.Lesson", "text", "text_enc", "lessons"),
    EncColumn("lessons.Lesson", "galaxy_note", "galaxy_note_enc", "lessons"),
    EncColumn("lessons.Lesson", "context", "context_enc", "lessons"),
    EncColumn("lessons.TutoringSession", "messages", "messages_enc", "tutoring_sessions", is_json=True),
    EncColumn(
        "lessons.TutoringSession",
        "connections_made",
        "connections_made_enc",
        "tutoring_sessions",
        is_json=True,
    ),
    EncColumn("lessons.StarJournalEntry", "text", "text_enc", "star_journal_entries"),
)
