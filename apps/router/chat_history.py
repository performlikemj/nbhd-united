"""Flat, cursor-paginated cross-channel chat history for rich clients (iOS).

The ``GET /api/v1/chat/messages/?since=<cursor>`` feed that lets the iOS app
surface turns from EVERY channel — its own (``AppChatMessage``), Telegram/LINE
(``ConversationTurn``), and cron / proactive sends (``ProactiveOutbound``) — in
one ascending, dedup-able stream. Deliberately stateless and replica-safe: the
cursor is a ``(created_at, id)`` keyset watermark derived from the DB clock, not
a per-replica offset (see ``CONTINUITY_REALTIME_CHAT_BACKEND_DIRECTIVE.md`` W2).

Shape per message row (the contract iOS dedups/merges against):

    {
      "id": "<stable, globally-unique>",   # primary dedup key (remoteId)
      "client_msg_id": "<id>",             # BOTH rows of a device-originated turn; absent on other channels
      "role": "user" | "assistant",
      "text": "<markdown, PII-rehydrated>",
      "created_at": "<ISO8601 UTC>",
      "source": "app" | "telegram" | "line" | "cron",
      "thread_id": "<stable thread id>",
    }

Design notes
------------
* **Role split.** One stored turn (a user message + an assistant reply in a
  single ``AppChatMessage`` / ``ConversationTurn`` row) is emitted as up to TWO
  message rows — a ``user`` row and an ``assistant`` row — each with its own
  stable id. A ``ProactiveOutbound`` (cron) is a single ``assistant`` row.
* **client_msg_id.** Carried on BOTH the user and assistant rows of a turn the
  device originated (``AppChatMessage``), so the client — which writes both rows
  optimistically before any server id exists — can dedup each by
  ``(client_msg_id, role)`` instead of double-inserting. Absent on other-channel
  rows (Telegram/LINE/cron), which the device never wrote locally.
* **Ordering.** Both rows of a turn key off the turn's ``created_at``; the
  synthetic id suffix (``:0`` user, ``:1`` assistant) breaks the tie so the user
  row always precedes its reply. The sort key ``(created_at, id)`` is a TOTAL,
  deterministic order across replicas (id is globally unique + source-prefixed).
* **PII.** Every served text is already user-safe at rest: ``AppChatMessage``
  / ``ConversationTurn`` replies are rehydrated + marker-stripped when stored,
  ``ProactiveOutbound`` is stored post-rehydration, and user text is the user's
  own words. No extra rehydration here.
* **thread_id.** ``ConversationTurn`` / ``ProactiveOutbound`` have no thread FK
  (OpenClaw keeps one flat rolling session per channel-user), so they are mapped
  to the tenant's single ``is_main`` thread — the shared thread every channel
  resumes. ``AppChatMessage`` rows carry their real thread id.
* **Backdating.** ``ChatLocalTurnView`` backdates an outbox-delayed on-device
  turn's ``created_at``; such a row written behind an already-served watermark
  is skipped by this strictly-monotonic feed. This is benign: backdated rows are
  ALWAYS device-originated (``source=on_device``), so the device already holds
  them locally — the feed exists to surface OTHER channels, which never backdate.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import UTC, datetime

from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

# Server-bounded page size. iOS loops via the cursor until a page is empty.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100

# "From the beginning" floor — older than any real row.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Opaque cursor: base64(json([created_at_iso, id])). Monotonic + replica-safe.
# ---------------------------------------------------------------------------


def encode_cursor(created_at: datetime, row_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), row_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str | None) -> tuple[datetime, str]:
    """Decode a cursor to a ``(created_at, id)`` watermark.

    Absent / malformed cursors fall back to the beginning rather than erroring:
    iOS never advances on an error, so a 4xx here would wedge the client in a
    retry loop. A lenient full re-read is harmless — iOS dedups by id.
    """
    if not cursor:
        return _EPOCH, ""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError("cursor is not a [created_at, id] pair")
        dt = parse_datetime(decoded[0])
        row_id = str(decoded[1])
        if dt is None:
            raise ValueError("unparsable cursor datetime")
        return dt, row_id
    # LookupError covers a JSON object/short list (KeyError/IndexError); the rest
    # cover bad base64 / non-JSON / wrong-typed payloads. Any malformed cursor
    # restarts from the beginning rather than 4xx-ing the polling client.
    except (ValueError, TypeError, LookupError, binascii.Error, json.JSONDecodeError):
        logger.warning("chat_history: malformed cursor, restarting from beginning")
        return _EPOCH, ""


# ---------------------------------------------------------------------------
# Row builders — one stored turn → up to two message rows.
# ---------------------------------------------------------------------------


def _row(*, row_id, created_at, role, text, source, thread_id, client_msg_id=None):
    """A single message row + its (created_at, id) sort key.

    ``_sort`` is stripped before the row leaves the view; it only drives the
    keyset ordering / cursor.
    """
    msg = {
        "id": row_id,
        "role": role,
        "text": text or "",
        "created_at": created_at.isoformat(),
        "source": source,
        "thread_id": str(thread_id),
    }
    if client_msg_id:
        msg["client_msg_id"] = client_msg_id
    return {"_sort": (created_at, row_id), "msg": msg}


def _app_rows(m, main_thread_id):
    """An ``AppChatMessage`` → a user row (always, if there's user text) and an
    assistant row (only once the reply has actually landed)."""
    from apps.router.models import AppChatMessage

    thread_id = str(m.thread_id) if m.thread_id else main_thread_id
    out = []
    if (m.user_text or "").strip():
        out.append(
            _row(
                row_id=f"app:{m.id}:0",
                created_at=m.created_at,
                role="user",
                text=m.user_text,
                source="app",
                thread_id=thread_id,
                client_msg_id=m.client_msg_id,  # device-originated → echo for dedup
            )
        )
    if m.status == AppChatMessage.Status.READY and (m.reply_text or "").strip():
        out.append(
            _row(
                row_id=f"app:{m.id}:1",
                created_at=m.created_at,
                role="assistant",
                text=m.reply_text,
                source="app",
                thread_id=thread_id,
                # Both halves of a device-originated turn carry the originating
                # client_msg_id as a dedup correlation key: the client wrote BOTH
                # rows optimistically (no server id yet), so the assistant row also
                # needs a shared key for the merge to backfill it instead of
                # inserting a duplicate. The client dedups by (client_msg_id, role).
                client_msg_id=m.client_msg_id,
            )
        )
    return out


def _conv_rows(t, main_thread_id):
    """A ``ConversationTurn`` (Telegram/LINE) → a user row + an assistant row."""
    out = []
    if (t.user_text or "").strip():
        out.append(
            _row(
                row_id=f"conv:{t.id}:0",
                created_at=t.created_at,
                role="user",
                text=t.user_text,
                source=t.channel,  # "telegram" | "line"
                thread_id=main_thread_id,
            )
        )
    if (t.reply_text or "").strip():
        out.append(
            _row(
                row_id=f"conv:{t.id}:1",
                created_at=t.created_at,
                role="assistant",
                text=t.reply_text,
                source=t.channel,
                thread_id=main_thread_id,
            )
        )
    return out


def _proactive_rows(p, main_thread_id):
    """A ``ProactiveOutbound`` (cron / proactive send) → one assistant row."""
    if not (p.message_text or "").strip():
        return []
    return [
        _row(
            row_id=f"cron:{p.id}",
            created_at=p.created_at,
            role="assistant",
            text=p.message_text,
            source="cron",
            thread_id=main_thread_id,
        )
    ]


# ---------------------------------------------------------------------------
# The page query.
# ---------------------------------------------------------------------------


def _page_slice(base_qs, after_dt, fetch):
    """One table's rows for a page: the FULL same-timestamp cluster at the
    watermark, plus the next ``fetch`` rows strictly after it — in ONE round trip.

    The boundary slice (``created_at == after_dt``) is never truncated: a cluster
    of rows sharing one microsecond — e.g. an offline outbox flushing several
    on-device turns backdated to a single ``occurred_at`` — must be returned in
    full so its tail is paged through, not skipped. (The SQL window can only
    advance by ``created_at``; the ``(created_at, id)`` keyset tiebreak that
    separates cluster members runs in Python in ``build_since_page``.)

    Boundary and forward are disjoint (``== after_dt`` vs ``> after_dt``), so they
    fold into a single ``UNION ALL`` — one cross-Pacific round trip per table
    instead of two (the DB is in Sydney; see ``production.py`` keepalives note).
    The from-the-beginning epoch watermark has a provably-empty boundary, so it
    skips the union and stays a single bounded forward read. Row order out of the
    union is unspecified, but ``build_since_page`` re-sorts by the synthetic
    ``(created_at, id)`` key, so it doesn't matter here.
    """
    forward = base_qs.filter(created_at__gt=after_dt).order_by("created_at", "id")[:fetch]
    if after_dt == _EPOCH:
        return list(forward)
    boundary = base_qs.filter(created_at=after_dt)
    return list(boundary.union(forward, all=True))


def build_since_page(tenant, main_thread_id: str, *, cursor: str | None, limit: int):
    """Return ``(messages, next_cursor)`` for one ascending page after ``cursor``.

    Unions the three channel tables, expands each stored turn to its role rows,
    drops anything at/behind the watermark, and returns the earliest ``limit``.
    ``next_cursor`` is the last row's keyset on a full page; on an empty page the
    incoming cursor is echoed back unchanged so iOS does not advance (and so a
    boundary row can never be skipped).
    """
    from apps.router.models import AppChatMessage, ConversationTurn, ProactiveOutbound

    limit = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    after_dt, after_id = decode_cursor(cursor)

    # Fetch each table's contribution as a boundary slice + a forward slice (see
    # _page_slice): the boundary materializes the FULL same-timestamp cluster at
    # the watermark so a tie-cluster larger than `fetch` is drained over
    # successive pages instead of being silently skipped (the SQL window can only
    # advance by created_at; the (created_at, id) tiebreak runs in Python below).
    fetch = limit + 1
    candidates = []

    # No ``.only()``: deferred fields interact awkwardly with ``.union()`` (a
    # deferred attr touched on a union row reloads it lazily), and every column
    # here is small — the texts are the bulk and we need those anyway — so the
    # saved bytes are dwarfed by the round trip we drop.
    app_qs = AppChatMessage.objects.filter(tenant=tenant)
    for m in _page_slice(app_qs, after_dt, fetch):
        candidates.extend(_app_rows(m, main_thread_id))

    conv_qs = ConversationTurn.objects.filter(tenant=tenant)
    for t in _page_slice(conv_qs, after_dt, fetch):
        candidates.extend(_conv_rows(t, main_thread_id))

    pro_qs = ProactiveOutbound.objects.filter(tenant=tenant)
    for p in _page_slice(pro_qs, after_dt, fetch):
        candidates.extend(_proactive_rows(p, main_thread_id))

    # Keyset filter: strictly after the watermark (so the cursor's own row isn't
    # re-served, and same-timestamp rows with a greater id are not skipped).
    watermark = (after_dt, after_id)
    fresh = [c for c in candidates if c["_sort"] > watermark]
    fresh.sort(key=lambda c: c["_sort"])

    page = fresh[:limit]
    if not page:
        # Empty page → don't advance. Echo the caller's cursor (or null).
        return [], cursor

    last = page[-1]["_sort"]
    next_cursor = encode_cursor(last[0], last[1])
    return [c["msg"] for c in page], next_cursor
