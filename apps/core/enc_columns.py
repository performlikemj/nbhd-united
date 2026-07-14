"""AAD coordinates + ladder group for the encrypted core (mindfulness) columns.

Encryption-at-rest Phase 3 (plan §1.2, §3). Same contract as
``apps.journal.enc_columns`` — byte-stable AAD, never hand-typed. Core shares the
journal flag pair (``Tenant.encrypt_journal_writes`` / ``read_encrypted_journal``,
plan §3.1); the completeness predicate enumerates ``CORE_ENC_COLUMNS`` under it.

``MeditationSession.manifest`` is the render manifest (phases -> segments); its
segments carry the same personalized narration text that ``guidance_text``
flattens, so it is sealed alongside ``guidance_text``/``theme`` — leaving it
plaintext would leak the exact narration the other two protect (plan §1.2/§1.5).

EXCLUDED: ``MeditationSession.error`` (system diagnostic) and structured
scalar prefs — OUT.
"""

from __future__ import annotations

from apps.crypto.enc_columns import EncColumn

# ── AAD 2-tuples (table, logical column) ─────────────────────────────────────
# core_profiles
CORE_PROFILE_ADDITIONAL_CONTEXT: tuple[str, str] = ("core_profiles", "additional_context")
# core_meditation_sessions
MEDITATION_SESSION_FEEDBACK_NOTE: tuple[str, str] = ("core_meditation_sessions", "feedback_note")
MEDITATION_SESSION_TITLE: tuple[str, str] = ("core_meditation_sessions", "title")
MEDITATION_SESSION_THEME: tuple[str, str] = ("core_meditation_sessions", "theme")
MEDITATION_SESSION_MANIFEST: tuple[str, str] = ("core_meditation_sessions", "manifest")
MEDITATION_SESSION_GUIDANCE_TEXT: tuple[str, str] = ("core_meditation_sessions", "guidance_text")

# ── Ladder group — both models carry a direct ``tenant`` FK. ─────────────────
CORE_ENC_COLUMNS: tuple[EncColumn, ...] = (
    EncColumn("core.CoreProfile", "additional_context", "additional_context_enc", "core_profiles"),
    EncColumn("core.MeditationSession", "feedback_note", "feedback_note_enc", "core_meditation_sessions"),
    EncColumn("core.MeditationSession", "title", "title_enc", "core_meditation_sessions"),
    EncColumn("core.MeditationSession", "theme", "theme_enc", "core_meditation_sessions"),
    EncColumn("core.MeditationSession", "manifest", "manifest_enc", "core_meditation_sessions", is_json=True),
    EncColumn("core.MeditationSession", "guidance_text", "guidance_text_enc", "core_meditation_sessions"),
)
