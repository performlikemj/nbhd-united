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
      "has_image": bool,                   # always present; true only on a user app row whose turn carried an image
      "has_document": bool,                # always present; true only on a user app row whose turn carried a PDF
      "user_redactions": [{"placeholder", "value"}],   # optional; user rows only
      "reply_redactions": [{"placeholder", "value"}],  # optional; assistant rows only
      "quick_replies": ["Label A", "Label B"],          # optional; assistant rows only, iOS-only
      "journal_link": {"kind", "slug", "title"},         # optional; assistant rows only (app + cron), iOS-only
    }

Both ``*_redactions`` keys are OMITTED when nothing was obfuscated (and are only
ever present on ``source="app"`` rows), so pre-feature clients see the unchanged
shape. See ``apps.router.pending_queue.placeholder_redactions``.

The ``has_image`` / ``has_document`` flags are ALWAYS emitted on every row
(matching the per-message detail path, which always carries them) so the wire
shape stays uniform: both false everywhere except a user ``source="app"`` row
whose turn carried an inbound image/PDF. They are computed from
``AppChatMessage.attachment_flags`` — the single source of truth shared with the
detail serializer so the two rendering paths can never drift. Older iOS builds
ignore the keys; the media-upload client reads them to render an attachment
placeholder bubble for the user's own turns across devices/reinstalls.

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
* **PII.** Assistant-authored text rests in PII-placeholder space —
  ``AppChatMessage.reply_text``, ``ConversationTurn.reply_text`` and
  ``ProactiveOutbound.message_text`` are stored marker-stripped but NOT
  rehydrated (pseudonymize-at-rest). This feed is owner-facing, so the row
  builders rehydrate those fields to real values on the way out (a no-op on
  legacy rows already stored with real names). ``AppChatMessage.user_text`` is
  the user's own words (verbatim); Telegram/LINE ``user_text`` is already
  placeholder-space and left as-is.
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

from apps.orchestrator.first_session_welcome import FIRST_SESSION_WELCOME_JOB_NAME

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


def _rehydrate(text, entity_map, *, tenant_id=None, channel="ios_feed"):
    """Rehydrate ``[TYPE_N]`` placeholders for an owner-facing feed row.

    Assistant-authored text (``AppChatMessage.reply_text`` /
    ``ConversationTurn.reply_text`` / ``ProactiveOutbound.message_text``) rests
    in PII-placeholder space; this feed serves the OWNER, so the placeholders
    are resolved to real values here. A no-op on legacy rows already stored with
    real names (no placeholders present) and when the tenant has no map, so the
    dual-read is transparent in both directions. Fail-open: any error serves the
    text unchanged rather than dropping the row from the feed."""
    from apps.router.reply_text import finalize_outbound_text

    return finalize_outbound_text(
        text,
        entity_map,
        tenant_id=tenant_id,
        channel=channel,
    )


def _row(
    *,
    row_id,
    created_at,
    role,
    text,
    source,
    thread_id,
    client_msg_id=None,
    has_image=False,
    has_document=False,
    user_redactions=None,
    reply_redactions=None,
    quick_replies=None,
    journal_link=None,
):
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
        # Attachment presence for the turn — ALWAYS emitted (mirrors the
        # per-message detail path, which always carries the keys). Both false on
        # every row except a user app row whose turn carried an inbound
        # image/PDF; the new iOS client reads them to render a placeholder
        # bubble across devices/reinstalls, older builds ignore them.
        "has_image": has_image,
        "has_document": has_document,
    }
    if client_msg_id:
        msg["client_msg_id"] = client_msg_id
    # Per-turn PII transparency metadata (optional; only AppChatMessage rows
    # carry it). Omitted entirely when nothing was obfuscated so older iOS
    # builds — which ignore the keys anyway — see the same shape as before.
    if user_redactions:
        msg["user_redactions"] = user_redactions
    if reply_redactions:
        msg["reply_redactions"] = reply_redactions
    # Quick-reply button labels (iOS-only; assistant rows only). Omitted
    # entirely when empty, same convention as the redaction keys above —
    # older iOS builds ignore unknown keys anyway.
    if quick_replies:
        msg["quick_replies"] = quick_replies
    # "View in Journal" deep-link (iOS-only; assistant rows only — app + cron).
    # Omitted entirely when empty, same convention as above.
    if journal_link:
        msg["journal_link"] = journal_link
    return {"_sort": (created_at, row_id), "msg": msg}


def _app_rows(m, main_thread_id, entity_map=None, *, user_text=None):
    """An ``AppChatMessage`` → a user row (always, if there's user text) and an
    assistant row (only once the reply has actually landed).

    ``reply_text`` is stored placeholder-space and rehydrated here (owner-facing).
    ``user_text`` is the user's own typed words — served verbatim, no rehydration.
    ``build_since_page`` pre-decrypts the page's user_text once (one bulk read,
    owner_request) and passes each ``RedactedStr`` in; we ``.reveal()`` it at the
    row-build egress. The emptiness check runs on the decrypted value so a
    ``b""``-sealed empty turn is dropped exactly like a legacy empty one."""
    from apps.crypto.nolog import RedactedStr
    from apps.router.journal_link import rehydrate_journal_link
    from apps.router.models import AppChatMessage
    from apps.router.quick_replies import rehydrate_quick_replies

    if user_text is None:
        user_text = RedactedStr(m.user_text)
    thread_id = str(m.thread_id) if m.thread_id else main_thread_id
    # The attachment belongs to the user's inbound turn, so the flags ride the
    # USER row; the assistant reply row (and every other channel's rows) keep
    # _row's False defaults. Shared source of truth with the detail path.
    has_image, has_document = m.attachment_flags
    out = []
    if (user_text or "").strip():
        out.append(
            _row(
                row_id=f"app:{m.id}:0",
                created_at=m.created_at,
                role="user",
                text=user_text.reveal(),
                source="app",
                thread_id=thread_id,
                client_msg_id=m.client_msg_id,  # device-originated → echo for dedup
                has_image=has_image,
                has_document=has_document,
                user_redactions=m.user_redactions,
            )
        )
    # Also emit a bare marker-only assistant row in the (expected rare) case the
    # agent's entire final reply was a marker line — reply_text strips to empty,
    # but the quick-reply buttons / journal-link chip must not silently vanish.
    if m.status == AppChatMessage.Status.READY and ((m.reply_text or "").strip() or m.quick_replies or m.journal_link):
        out.append(
            _row(
                row_id=f"app:{m.id}:1",
                # Sort the reply by when it LANDED, not when the user asked.
                # Stamped with the user-turn's created_at, a slow reply (12-165s
                # observed) that completes after an interleaved cron/proactive
                # row has already advanced a client's since-cursor falls BEHIND
                # the strictly-monotonic watermark and is never served — a
                # permanent drop for any client relying on the since feed
                # (second device, reinstall, or a device whose in-flight poll
                # died in the background).
                created_at=m.replied_at or m.created_at,
                role="assistant",
                text=_rehydrate(m.reply_text, entity_map, tenant_id=m.tenant_id),
                source="app",
                thread_id=thread_id,
                # Both halves of a device-originated turn carry the originating
                # client_msg_id as a dedup correlation key: the client wrote BOTH
                # rows optimistically (no server id yet), so the assistant row also
                # needs a shared key for the merge to backfill it instead of
                # inserting a duplicate. The client dedups by (client_msg_id, role).
                client_msg_id=m.client_msg_id,
                reply_redactions=m.reply_redactions,
                # Stored PLACEHOLDER-space (parsed before reply_text is
                # rehydrated) — rehydrate via the SAME shared helper the
                # detail seam calls (chat_views._serialize_message) so a
                # stored "[PERSON_1]" label can't ship raw at one seam and
                # rehydrated at the other.
                quick_replies=rehydrate_quick_replies(
                    m.quick_replies, entity_map, tenant_id=m.tenant_id, channel="ios_feed"
                ),
                # Same as quick_replies: stored placeholder-space (title parsed
                # before reply_text is rehydrated), rehydrated via the SAME
                # shared helper the detail seam calls so the two can't drift.
                journal_link=rehydrate_journal_link(
                    m.journal_link, entity_map, tenant_id=m.tenant_id, channel="ios_feed"
                ),
            )
        )
    return out


def _conv_rows(t, main_thread_id, entity_map=None):
    """A ``ConversationTurn`` (Telegram/LINE) → a user row + an assistant row.

    ``reply_text`` is stored placeholder-space and rehydrated here (owner-facing).
    ``user_text`` for Telegram/LINE is already placeholder-space at rest and is
    left as-is — that pre-existing behaviour is out of this change's scope."""
    out = []
    if (t.user_text or "").strip():
        out.append(
            _row(
                row_id=f"conv:{t.id}:0",
                created_at=t.created_at,
                role="user",
                text=_rehydrate(
                    t.user_text,
                    entity_map,
                    tenant_id=t.tenant_id,
                    channel="ios_feed_conversation_user",
                ),
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
                text=_rehydrate(t.reply_text, entity_map, tenant_id=t.tenant_id),
                source=t.channel,
                thread_id=main_thread_id,
            )
        )
    return out


def _proactive_rows(p, main_thread_id, entity_map=None):
    """A ``ProactiveOutbound`` (cron / proactive send) → one assistant row.

    ``message_text`` is stored placeholder-space and rehydrated here (owner-facing).
    ``quick_replies`` and a ``journal_link`` ride the row too — rehydrated via
    the same shared helpers the app-message seams use so proactive and app-reply
    affordances resolve stored placeholder text identically."""
    from apps.router.journal_link import rehydrate_journal_link
    from apps.router.quick_replies import rehydrate_quick_replies

    # Keep a marker-only row: stripping can legitimately leave no prose, but
    # the pills/chip are still the proactive payload the app needs to render.
    if not (p.message_text or "").strip() and not p.quick_replies and not p.journal_link:
        return []
    return [
        _row(
            row_id=f"cron:{p.id}",
            created_at=p.created_at,
            role="assistant",
            text=_rehydrate(p.message_text, entity_map, tenant_id=p.tenant_id),
            source="cron",
            thread_id=main_thread_id,
            quick_replies=rehydrate_quick_replies(
                p.quick_replies, entity_map, tenant_id=p.tenant_id, channel="cron_feed"
            ),
            journal_link=rehydrate_journal_link(p.journal_link, entity_map, tenant_id=p.tenant_id, channel="cron_feed"),
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
    from apps.router import enc_columns, enc_read
    from apps.router.models import AppChatMessage, ConversationTurn, ProactiveOutbound

    limit = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    after_dt, after_id = decode_cursor(cursor)

    # Assistant-authored text (reply_text / message_text) rests placeholder-space;
    # this feed is owner-facing, so the row builders rehydrate it. One map read,
    # threaded to every builder.
    entity_map = getattr(tenant, "pii_entity_map", None)

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
    # The app table's window must ALSO admit rows whose reply landed after the
    # watermark: the assistant half sorts by replied_at (see _app_rows), and a
    # slow reply completing after an interleaved cron advanced the cursor lives
    # on a row whose created_at is already BEHIND the watermark — a pure
    # created_at window would never fetch it and the reply would be permanently
    # unservable through this feed.
    from django.db.models import Q

    app_qs = AppChatMessage.objects.filter(tenant=tenant)
    app_forward = app_qs.filter(Q(created_at__gt=after_dt) | Q(replied_at__gt=after_dt)).order_by("created_at", "id")[
        :fetch
    ]
    if after_dt == _EPOCH:
        app_slice = list(app_forward)
    else:
        app_boundary = app_qs.filter(Q(created_at=after_dt) | Q(replied_at=after_dt))
        # all=False: a row can match boundary on one timestamp and forward on
        # the other; the deduping union keeps it single.
        app_slice = list(app_boundary.union(app_forward))
    # Bulk-decrypt the app slice's user_text ONCE (one audit event, row_count=N,
    # owner_request — amendment b), then hand each RedactedStr to _app_rows. The
    # slice loads all columns (no .only(), see above), so user_text_enc is present.
    app_user_texts = enc_read.read_values_bulk(
        tenant,
        enc_columns.APP_CHAT_MESSAGE_USER_TEXT,
        [(m.user_text_enc, m.user_text) for m in app_slice],
    )
    for m, ut in zip(app_slice, app_user_texts):
        candidates.extend(_app_rows(m, main_thread_id, entity_map, user_text=ut))

    conv_qs = ConversationTurn.objects.filter(tenant=tenant)
    for t in _page_slice(conv_qs, after_dt, fetch):
        candidates.extend(_conv_rows(t, main_thread_id, entity_map))

    # Eval rows are internal probe evidence, never owner-facing feed messages.
    # The first-session job's USER-VISIBLE delivery is the AppChatMessage; its
    # ProactiveOutbound row is audit-only.
    pro_qs = (
        ProactiveOutbound.objects.filter(tenant=tenant)
        .exclude(channel=ProactiveOutbound.Channel.EVAL)
        .exclude(job_name=FIRST_SESSION_WELCOME_JOB_NAME)
    )
    for p in _page_slice(pro_qs, after_dt, fetch):
        candidates.extend(_proactive_rows(p, main_thread_id, entity_map))

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
