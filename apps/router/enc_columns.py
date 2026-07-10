"""Single source of the ``(table, column)`` AAD coordinates for encrypted chat columns.

Encryption-at-rest Phase 2. ``box.encrypt`` / ``box.decrypt`` bind every
ciphertext to ``(tenant_id, table, column)`` through the AAD
(``apps.crypto.codec.build_aad``). Those ``table`` / ``column`` strings must be
**byte-identical at every encrypt site and every decrypt site, forever** — a
typo or drift silently makes previously-written rows fail the GCM auth check and
become undecryptable (fail-closed, but unrecoverable). So every producer and
consumer imports these tuples; NEVER hand-type the strings.

Each tuple is the AAD coordinate, NOT the physical storage column:
  - ``table`` matches the model's ``db_table``.
  - ``column`` names the LOGICAL value the ciphertext represents (``user_text``,
    ``title``) — deliberately the plaintext column's name, not the ``*_enc``
    column that stores the envelope. Keeping the logical name means a future
    contract migration that drops the plaintext column doesn't have to re-key
    (and re-encrypt) every row.

Usage: ``box.encrypt(tenant_id, *enc_columns.APP_CHAT_MESSAGE_USER_TEXT, value)``.
"""

from __future__ import annotations

# AppChatMessage.user_text  ->  AppChatMessage.user_text_enc
APP_CHAT_MESSAGE_USER_TEXT: tuple[str, str] = ("app_chat_messages", "user_text")

# ChatThread.title  ->  ChatThread.title_enc
CHAT_THREAD_TITLE: tuple[str, str] = ("chat_threads", "title")
