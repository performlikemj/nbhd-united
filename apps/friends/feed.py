"""Keyset ``?since=`` feed for friend chat — forks the opaque-cursor pattern of
``apps/router/chat_history`` (design §6).

``FriendMessage`` has a monotonic ``BigAutoField`` ``seq``, so a single-column
keyset (``seq > after``) is a clean total order — none of the ``(created_at,
id)`` tiebreak / three-table-union complexity the main chat feed needs. Poll is
truth; the cursor never advances on an empty page (so a boundary row can't be
skipped) and a malformed cursor restarts from the beginning rather than 4xx-ing
a polling client. The ``FriendMessage`` query itself lives in
:mod:`apps.friends.access` (chokepoint); this module owns only the cursor +
serialization.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging

from . import access

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(json.dumps(int(seq)).encode()).decode()


def decode_cursor(cursor) -> int:
    """Decode a cursor to an ``after_seq`` watermark. Absent / malformed cursors
    fall back to 0 (full re-read) rather than erroring — a polling client never
    advances on a 4xx, so a lenient restart is safer (the client dedups by seq)."""
    if not cursor:
        return 0
    try:
        value = json.loads(base64.urlsafe_b64decode(str(cursor).encode()))
        return max(0, int(value))
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        logger.warning("friend feed: malformed cursor, restarting from beginning")
        return 0


def build_thread_page(viewer_tenant, thread, *, cursor, limit) -> tuple[list[dict], str | None]:
    """Return ``(messages, next_cursor)`` for one ascending page after ``cursor``.
    On an empty page the incoming cursor is echoed back so the client doesn't
    advance. The caller must have run :func:`access.assert_participant` first."""
    after = decode_cursor(cursor)
    limit = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    viewer_id = getattr(viewer_tenant, "id", viewer_tenant)
    messages = access.thread_messages_page(thread, after, limit, viewer_tenant_id=viewer_id)
    items = [
        {
            "public_id": str(m.public_id),
            "seq": m.seq,
            "text": m.text,  # raw human text — verbatim human↔human (design §4.6)
            "mine": m.sender_tenant_id == viewer_id,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]
    next_cursor = encode_cursor(messages[-1].seq) if messages else cursor
    return items, next_cursor
