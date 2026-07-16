"""Shared chat-encryption convergence predicate (Encryption-at-rest Phase 2).

Single home for the completeness check both convergence surfaces gate the
read-flip on — ``provision_tenant`` (fresh-tenant compressed ladder) and the
``converge_unencrypted_chat_tenants`` command (post-flip cohort ladder). The
predicate must be byte-for-byte the same question in both places: a tenant may
only have ``read_encrypted_chat`` flipped ON when it has ZERO plaintext-only
content rows, i.e. rows the irreversible PR-6 erase would destroy as the only
copy. Keeping it here (rather than duplicated in two files) means the two
gates can never drift apart.
"""

from __future__ import annotations

from apps.router.models import AppChatMessage, ChatThread

# (model, plaintext field, _enc field) for the two Phase-2 in-scope chat columns.
CHAT_ENC_COLUMNS = (
    (AppChatMessage, "user_text", "user_text_enc"),
    (ChatThread, "title", "title_enc"),
)


def count_unsealed_chat_rows(tenant) -> int:
    """Rows still plaintext-only for this tenant: ``_enc IS NULL AND legacy <> ''``.

    The plan §7 completeness predicate (docs/encryption-at-rest-phase2-plan.md).
    Empty legacy rows are excluded: NULL ``_enc`` + ``""`` legacy reads as ``""``
    through the dual-read fallback either way, so they carry no content the
    erase could destroy and never block a read-flip. Returns a COUNT only —
    never fetches content.
    """
    remaining = 0
    for model, value_field, enc_field in CHAT_ENC_COLUMNS:
        remaining += (
            model.objects.filter(tenant=tenant, **{f"{enc_field}__isnull": True}).exclude(**{value_field: ""}).count()
        )
    return remaining
