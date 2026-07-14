"""AAD coordinates + ladder group for the encrypted insights columns.

Encryption-at-rest Phase 3 (plan §1.2, §3). Same contract as
``apps.journal.enc_columns`` — byte-stable AAD, never hand-typed. Insights share
the journal flag pair (``Tenant.encrypt_journal_writes`` / ``read_encrypted_journal``,
plan §3.1); the completeness predicate enumerates ``INSIGHTS_ENC_COLUMNS`` under it.

EXCLUDED: ``TopicRegistry.description`` (ops-authored taxonomy) and
``PillarSnapshot.payload`` (computed render mirror) — structured/OUT (plan §1.2).
"""

from __future__ import annotations

from apps.crypto.enc_columns import EncColumn

# ── AAD 2-tuples (table, logical column) ─────────────────────────────────────
# insights_assistant_insight
ASSISTANT_INSIGHT_STATEMENT: tuple[str, str] = ("insights_assistant_insight", "statement")
ASSISTANT_INSIGHT_USER_RESPONSES: tuple[str, str] = ("insights_assistant_insight", "user_responses")

# ── Ladder group — AssistantInsight carries a direct ``tenant`` FK. ──────────
INSIGHTS_ENC_COLUMNS: tuple[EncColumn, ...] = (
    EncColumn("insights.AssistantInsight", "statement", "statement_enc", "insights_assistant_insight"),
    EncColumn(
        "insights.AssistantInsight",
        "user_responses",
        "user_responses_enc",
        "insights_assistant_insight",
        is_json=True,
    ),
)
