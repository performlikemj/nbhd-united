"""JWT-authed chat ingress for rich clients (iOS/web) that route through
the tenant's OpenClaw runtime.

Unlike Telegram/LINE (push channels), these clients have no push transport,
so the assistant reply is persisted to ``AppChatMessage`` and the client
polls ``GET /api/v1/chat/messages/<client_msg_id>/`` for it.

A conversation is a first-class ``ChatThread`` (channel-independent). The
shared ``is_main`` thread is the default; clients may create additional
named threads. The OpenClaw ``user`` param is ``thread:<thread_id>`` so each
thread is its own OpenClaw session while ``USER.md``/memory stays shared —
which is what makes the assistant "know who you are" on a brand-new surface.

This is the additive, non-breaking iOS slice: it reuses the existing
``enqueue_message_for_tenant`` → drain → ``/v1/chat/completions`` path
(wake, lease, coalesce, reaper, usage all inherited). The drain's
``_drain_ios_batch`` fills in the reply. Telegram/LINE routing is untouched
here — pointing them at the shared main thread is a follow-up PR.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services import check_budget
from apps.crypto.nolog import RedactedStr
from apps.router import enc_columns, enc_read
from apps.router.conversation_capture import scrub_chat_thread_title
from apps.router.inbound_media import (
    MAX_APP_DOCUMENT_BYTES,
    MAX_APP_IMAGE_BYTES,
    attachment_marker,
    decode_and_validate_document,
    decode_and_validate_image,
    store_inbound_document,
    store_inbound_image,
)
from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import enqueue_message_for_tenant, placeholder_redactions
from apps.router.reply_text import clamp_reply_text
from apps.router.services import build_chat_context_marker, build_datetime_context
from apps.tenants.models import Tenant
from apps.tenants.throttling import (
    ChatContextHourThrottle,
    ChatLocalTurnHourThrottle,
    ChatMessageSendHourThrottle,
)

logger = logging.getLogger(__name__)

# Upper bound on a single inbound chat message. Generous for a chat UI but
# bounded so a pathological payload can't bloat the queue row / prompt.
_MAX_CHARS = 8000

# Hard ceiling on the raw request body. DRF's JSONParser reads the request
# stream directly, bypassing Django's DATA_UPLOAD_MAX_MEMORY_SIZE, so without
# this an authenticated client could POST a multi-hundred-MB base64 attachment
# and OOM the shared control plane. Sized to admit the largest allowed
# attachment (a 10 MB PDF; base64 ≈ 4/3 ⇒ ~13.4 MB) plus the JSON envelope + an
# 8k caption, and nothing beyond that. A too-large image still fails its own
# precise 400 (image_too_large) after decode; this coarse guard is the pre-body
# OOM defense only.
_MAX_REQUEST_BODY_BYTES = max(MAX_APP_IMAGE_BYTES, MAX_APP_DOCUMENT_BYTES) * 4 // 3 + 300_000

# ``AppChatMessage.client_msg_id`` is CharField(max_length=64); Django doesn't
# enforce that on save, so an oversized id would surface as a Postgres
# DataError (500). Reject it instead — truncating would silently change the
# idempotency key.
_CLIENT_MSG_ID_MAX = 64

# How far in the past a client-supplied ``occurred_at`` may sit (matches the
# ConversationTurn retention window) and how much clock skew into the future
# is tolerated.
_OCCURRED_AT_MAX_AGE = timedelta(days=35)
_OCCURRED_AT_MAX_SKEW = timedelta(minutes=5)


def _parse_occurred_at(raw: object) -> timezone.datetime | None:
    """Client-supplied 'when the turn actually happened' (ISO 8601).

    Fail-open: anything unparsable, naive, too old, or in the future is
    treated as absent and the row is stamped with delivery time.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = parse_datetime(text)
    except ValueError:
        return None
    if parsed is None or timezone.is_naive(parsed):
        return None
    now = timezone.now()
    if parsed > now + _OCCURRED_AT_MAX_SKEW or parsed < now - _OCCURRED_AT_MAX_AGE:
        return None
    return parsed


# How many messages a thread-history GET returns by default.
_HISTORY_LIMIT = 50


def _encrypt_chat_value(tenant, aad: tuple[str, str], value: str) -> bytes | None:
    """Dual-write seal of a chat column value (encryption-at-rest Phase 2, PR-2).

    Returns the sealed envelope bytes when ``tenant.encrypt_chat_writes`` is on,
    else ``None`` (plaintext-only row). ``aad`` is an ``enc_columns`` ``(table,
    column)`` tuple — never a hand-typed string (plan risk #6).

    Soft-fail BY DESIGN at this stage: a ``box.encrypt`` failure logs ONE
    count-only line (tenant prefix + column name, never content) and returns
    ``None`` so the row is still fully readable via its plaintext column. This is
    availability-correct ONLY while the plaintext column is still written
    alongside; PR-6 (erase) MUST invert this to fail-closed once writers go
    ``_enc``-only. See docs/encryption-at-rest-phase2-plan.md §5 PR-2/PR-6.
    """
    if not getattr(tenant, "encrypt_chat_writes", False):
        return None
    # Local import so tests can patch ``apps.crypto.box.encrypt`` at call time —
    # a module-level alias would bind before the patch and silently no-op it.
    from apps.crypto import box

    try:
        return box.encrypt(tenant.id, aad[0], aad[1], value)
    except Exception:
        logger.warning(
            "chat dual-write encrypt failed for tenant %s column %s — row stored plaintext-only",
            str(tenant.id)[:8],
            aad[1],
        )
        return None


def _get_or_create_main_thread(tenant, user) -> ChatThread:
    """The shared default thread every channel resumes. One per tenant —
    the partial unique constraint makes the get_or_create race-safe."""
    title = scrub_chat_thread_title("Main")
    thread, _ = ChatThread.objects.get_or_create(
        tenant=tenant,
        is_main=True,
        defaults={
            "user": user,
            "title": title,
            # Sealed only on CREATE (defaults are ignored on get) — matches the
            # column's insert-once contract.
            "title_enc": _encrypt_chat_value(tenant, enc_columns.CHAT_THREAD_TITLE, title),
        },
    )
    return thread


# A tenant's main-thread id changes only through an explicit admin action, so the
# read-heavy ?since= feed caches it rather than paying a get_or_create round trip
# to Sydney on every ~30s poll. The feed only needs the id (a label for non-app
# rows), not the object. Cache-aside: a miss (or any cache hiccup) falls straight
# through to the DB, and on the very first call still creates the thread. The
# cache is replaced on a main-thread swap here and busted on is_main thread
# deletion via apps/router/signals.py; the TTL is only a last-ditch bound.
_MAIN_THREAD_ID_TTL = 60 * 60


def _main_thread_cache_key(tenant_id) -> str:
    return f"nbhd:router:main_thread_id:{tenant_id}"


def _main_thread_id_cached(tenant, user) -> str:
    from django.core.cache import cache

    key = _main_thread_cache_key(tenant.id)
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001 — cache blip must never break the feed
        cached = None
    if cached:
        return cached
    tid = str(_get_or_create_main_thread(tenant, user).id)
    cache_main_thread_id(tenant.id, tid)
    return tid


def cache_main_thread_id(tenant_id, thread_id) -> None:
    """Cache the committed main-thread id for flat-feed attribution."""
    from django.core.cache import cache

    try:
        cache.set(_main_thread_cache_key(tenant_id), str(thread_id), _MAIN_THREAD_ID_TTL)
    except Exception:  # noqa: BLE001
        pass


def invalidate_main_thread_cache(tenant_id) -> None:
    """Drop the cached main-thread id after deletion."""
    from django.core.cache import cache

    try:
        cache.delete(_main_thread_cache_key(tenant_id))
    except Exception:  # noqa: BLE001
        pass


def _thread_user_param(thread: ChatThread) -> str:
    """OpenClaw ``user`` param for a thread → its own session, shared memory."""
    return f"thread:{thread.id}"


def _resolve_thread(request, tenant) -> ChatThread | None:
    """Resolve the target thread for a message. Empty/absent thread_id →
    the shared main thread. Returns None if a given thread_id doesn't
    resolve to one of this tenant's threads."""
    thread_id = str(request.data.get("thread_id") or "").strip()
    if not thread_id:
        return _get_or_create_main_thread(tenant, request.user)
    try:
        return ChatThread.objects.filter(tenant=tenant, id=thread_id).first()
    except (ValueError, ValidationError):
        return None


def _no_store(response):
    """Chat reads must never be served from an HTTP cache.

    Without an explicit header, ETagMiddleware stamps GETs with
    ``private, max-age=10`` — which lets a client's HTTP cache replay a
    stale "pending" poll body for up to 10s AFTER the reply is ready,
    adding that long to perceived chat latency.
    """
    response["Cache-Control"] = "no-store"
    return response


def _serialize_thread(thread: ChatThread, *, tenant=None) -> dict:
    # Owner-facing single-row read of the title (dual-read behind
    # read_encrypted_chat; audits under the ambient owner_request — no per-view
    # set_principal, amendment b). Callers with the tenant in hand pass it to
    # avoid a per-row FK read.
    tenant = tenant if tenant is not None else getattr(thread, "tenant", None)
    title = enc_read.read_value(tenant, enc_columns.CHAT_THREAD_TITLE, thread.title_enc, thread.title).reveal()
    return {
        "id": str(thread.id),
        "title": title,
        "is_main": thread.is_main,
        "created_at": thread.created_at.isoformat(),
        "last_active_at": thread.last_active_at.isoformat() if thread.last_active_at else None,
    }


def _serialize_message(msg: AppChatMessage, *, entity_map=None, user_text: RedactedStr | None = None) -> dict:
    # ``reply_text`` rests in PII-placeholder space (pseudonymize-at-rest); this
    # is an owner-facing serializer, so rehydrate it to real values on the way
    # out. A no-op on legacy rows already stored with real names and on
    # on-device (``ChatLocalTurnView``) replies authored in real-name space.
    # ``user_text`` is the user's own typed words — served verbatim. Callers with
    # the tenant in hand pass ``entity_map`` to avoid a per-row FK read; else it
    # resolves from ``msg.tenant`` (cached on freshly-created rows).
    if entity_map is None:
        entity_map = getattr(getattr(msg, "tenant", None), "pii_entity_map", None)
    # user_text: bulk callers (ChatThreadMessagesView) pre-decrypt the whole page
    # via one decrypt_bulk and pass the RedactedStr in; single-row callers
    # (poll/detail/post/on-device/siri) leave it None and we dual-read here, which
    # audits under the ambient owner_request (amendment b — no per-view principal).
    if user_text is None:
        user_text = enc_read.read_value(
            getattr(msg, "tenant", None),
            enc_columns.APP_CHAT_MESSAGE_USER_TEXT,
            msg.user_text_enc,
            msg.user_text,
        )
    from apps.router.reply_text import finalize_outbound_text

    reply_text = finalize_outbound_text(
        msg.reply_text,
        entity_map,
        tenant_id=msg.tenant_id,
        channel="ios_detail",
    )
    # One attachment per turn, typed off the stored file's extension. Shared
    # with the ?since= feed via AppChatMessage.attachment_flags so the two
    # rendering paths can't drift.
    has_image, has_document = msg.attachment_flags
    # quick_replies is stored PLACEHOLDER-space (parsed before reply_text is
    # rehydrated — see _clean_assistant_text_for_app); rehydrate here via the
    # SAME shared helper the ?since= feed calls (_app_rows) so a stored
    # "[PERSON_1]" label can't ship raw at one seam and rehydrated at the other.
    from apps.router.quick_replies import rehydrate_quick_replies

    quick_replies = rehydrate_quick_replies(
        msg.quick_replies, entity_map, tenant_id=msg.tenant_id, channel="ios_detail"
    )
    # journal_link.title is likewise stored PLACEHOLDER-space; rehydrate here via
    # the SAME shared helper the ?since= feed calls (_app_rows) so the two seams
    # can't drift on how a stored title resolves.
    from apps.router.journal_link import rehydrate_journal_link

    journal_link = rehydrate_journal_link(msg.journal_link, entity_map, tenant_id=msg.tenant_id, channel="ios_detail")
    return {
        "client_msg_id": msg.client_msg_id,
        "thread_id": str(msg.thread_id),
        "status": msg.status,
        "source": msg.source,
        "user_text": user_text.reveal(),
        "reply_text": reply_text,
        "error": msg.error,
        "created_at": msg.created_at.isoformat(),
        "replied_at": msg.replied_at.isoformat() if msg.replied_at else None,
        # Set while a hibernated container boots; clients show "waking up"
        # copy when status is still pending and this is non-null.
        "waking_at": msg.waking_at.isoformat() if msg.waking_at else None,
        # Live agent-activity narration (waking/thinking/using tool/composing)
        # — drives the in-app "thinking" state and the iOS-27 Live Activity.
        "phase": msg.phase,
        "phase_detail": msg.phase_detail,
        # Per-step partial assistant text (pseudo-streaming). Cumulative
        # text-so-far, keyed by a monotonic seq; the client renders it while the
        # turn is pending and switches to reply_text once status flips to ready.
        # Exposed only while pending — a ready/error row reports '' / 0 (the
        # partial is cleared with the final reply anyway).
        "partial_text": msg.partial_text if msg.status == AppChatMessage.Status.PENDING else "",
        "partial_seq": msg.partial_seq if msg.status == AppChatMessage.Status.PENDING else 0,
        # True when the user's turn carried an inbound image / PDF (stored on
        # the share; the raw path is internal and not exposed). Lets a polling
        # client render the right attachment bubble for the turn. Mutually
        # exclusive — see AppChatMessage.attachment_flags.
        "has_image": has_image,
        "has_document": has_document,
        # Per-turn PII transparency: the real values obfuscated behind
        # placeholders on the way to / back from the assistant, as
        # [{"placeholder", "value"}]. null when nothing was obfuscated or the
        # row predates the feature. Older iOS builds ignore the unknown keys.
        "user_redactions": msg.user_redactions,
        "reply_redactions": msg.reply_redactions,
        # Up to 3 tappable choice labels parsed from a trailing
        # [[quick-replies: A | B | C]] marker on the reply (iOS-only), REHYDRATED
        # to real values above. null when the turn carried no marker, or the
        # rehydrated labels overflowed the length cap (dropped, not truncated).
        "quick_replies": quick_replies,
        # A tappable "View in Journal" deep-link ({"kind", "slug", "title"})
        # parsed from a trailing [[journal-link: kind|slug|title]] marker on the
        # reply (iOS-only); title REHYDRATED above. null when the turn carried no
        # (valid) marker. Shared source of truth with the ?since= feed.
        "journal_link": journal_link,
    }


def enqueue_tenant_turn(
    *,
    tenant,
    user,
    text: str,
    thread: ChatThread,
    client_msg_id: str,
    image: bytes | None = None,
    image_ext: str = "jpg",
    document: bytes | None = None,
    document_ext: str = "pdf",
):
    """Create a PENDING ``AppChatMessage`` and enqueue a Tier-3 OpenClaw turn.

    The single chokepoint for "route this ask to the full tenant agent": used by
    both the normal ``ChatMessageView`` POST and the Tier-2 fast-responder
    escalation path (``apps.router.siri_views``). Idempotent on
    ``client_msg_id`` and budget-gated, exactly once.

    ``image`` / ``document`` (optional, already-decoded+validated bytes; at most
    one per turn) are stored on the tenant share and referenced from the
    LLM-bound text via ``inbound_media.attachment_marker`` — ``[Photo attached:
    <path> — ...]`` for an image (the same marker the Telegram poller uses;
    read by the built-in ``image`` tool) or ``[Document attached: <path> —
    ...]`` for a PDF (read by the built-in ``pdf`` tool). Both markers carry an
    untrusted-content framing (the attachment is third-party data, never
    instructions — see ``docs/upload-security-threat-model.md`` AC-1). The
    bytes never ride the queue payload. Storing happens AFTER the idempotency +
    budget gates so a replay or an over-budget turn does no share I/O.

    Returns ``(turn, created)`` — ``created`` is True only when a fresh PENDING
    turn was enqueued (so the caller can pick 201 vs 200). A budget-exhausted
    turn is recorded as ERROR and returned with ``created=False`` (nothing
    enqueued, no container woken).
    """
    # Defense in depth: one attachment per turn (attachment_path is a single
    # column). The ChatMessageView POST already enforces this XOR before calling
    # in; this guard makes the invariant local so a future caller can't silently
    # store two files and clobber attachment_path. (Neither the Siri escalation
    # path nor any current caller passes both.)
    if image is not None and document is not None:
        raise ValueError("enqueue_tenant_turn: pass at most one of image/document per turn")

    existing = AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).first()
    if existing:
        return existing, False

    # Dual-write ciphertext (Phase 2, PR-2): the same `text` seals both the
    # budget-exhausted and the normal turn below, so compute it once. None when
    # the tenant's write-flag is off.
    user_text_enc = _encrypt_chat_value(tenant, enc_columns.APP_CHAT_MESSAGE_USER_TEXT, text)

    # Budget gate — don't enqueue work (or wake a container) for an over-budget
    # tenant. Recorded as an error so the client surfaces the reason.
    budget_reason = check_budget(tenant)
    if budget_reason:
        try:
            turn = AppChatMessage.objects.create(
                tenant=tenant,
                user=user,
                thread=thread,
                client_msg_id=client_msg_id,
                user_text=text,
                user_text_enc=user_text_enc,
                status=AppChatMessage.Status.ERROR,
                error="budget_exhausted",
                replied_at=timezone.now(),
            )
        except IntegrityError:
            turn = AppChatMessage.objects.get(tenant=tenant, client_msg_id=client_msg_id)
        return turn, False

    try:
        turn = AppChatMessage.objects.create(
            tenant=tenant,
            user=user,
            thread=thread,
            client_msg_id=client_msg_id,
            user_text=text,
            user_text_enc=user_text_enc,
            status=AppChatMessage.Status.PENDING,
        )
    except IntegrityError:
        # Concurrent retry won the (tenant, client_msg_id) race; the winner
        # already enqueued the tenant turn — replay, don't re-enqueue.
        turn = AppChatMessage.objects.get(tenant=tenant, client_msg_id=client_msg_id)
        return turn, False

    user_tz = getattr(user, "timezone", None) or "UTC"

    # Inbound image: persist the bytes on the tenant share and decorate the
    # LLM-bound text with the same [Photo attached: <path>] marker the Telegram
    # poller uses, so the agent's built-in ``image`` tool reads the local file.
    # The marker is NOT PII and is NOT part of the user's displayed text, so it
    # is assembled separately from (and never fed through) redaction. If the
    # store fails we degrade to a text turn with a "couldn't process" marker —
    # mirrors the poller's >5 MB fallback — rather than dropping the turn.
    image_marker = ""
    if image:
        try:
            container_path, workspace_path = store_inbound_image(str(tenant.id), image, image_ext)
            AppChatMessage.objects.filter(pk=turn.pk).update(attachment_path=workspace_path)
            turn.attachment_path = workspace_path
            image_marker = attachment_marker("photo", container_path)
        except Exception:
            logger.exception(
                "enqueue_tenant_turn: image store failed for tenant %s — degrading to a text turn",
                str(tenant.id)[:8],
            )
            image_marker = "[The user attached a photo but it couldn't be processed — ask them to resend it.]\n"

    # Inbound PDF: same pattern as the photo above, but the marker is built by
    # attachment_marker("document", ...) so the agent's built-in ``pdf`` tool
    # reads the local file. The view enforces at most one attachment per turn,
    # so image and document never both write attachment_path in the same call.
    document_marker = ""
    if document:
        try:
            container_path, workspace_path = store_inbound_document(str(tenant.id), document, document_ext)
            AppChatMessage.objects.filter(pk=turn.pk).update(attachment_path=workspace_path)
            turn.attachment_path = workspace_path
            document_marker = attachment_marker("document", container_path)
        except Exception:
            logger.exception(
                "enqueue_tenant_turn: document store failed for tenant %s — degrading to a text turn",
                str(tenant.id)[:8],
            )
            document_marker = "[The user attached a document but it couldn't be processed — ask them to resend it.]\n"

    # PII redaction for outgoing LLM-provider traffic. Redact the bare user
    # text BEFORE prepending the datetime/chat markers (redacting the
    # assembled body makes the NER detector misfire on the structural
    # markers). We redact ONLY the LLM-bound payload — the user's own
    # AppChatMessage.user_text (persisted above) and the display excerpt stay
    # verbatim so the iOS ?since= feed shows exactly what the user typed.
    # Outbound rehydration is already wired in the drain path, so [PERSON_N]
    # placeholders round-trip. redact_user_message swallows its own errors
    # and returns the original text, so it never blocks delivery.
    from apps.pii.redactor import redact_user_message

    redacted_text = redact_user_message(text, tenant)
    # Per-turn transparency metadata: which of the user's real values were
    # obfuscated behind placeholders before this turn reached the assistant.
    # redact_user_message has already minted+persisted any new bindings onto
    # tenant.pii_entity_map (in-memory too), so resolving the redacted text's
    # placeholders against it now finds even freshly-minted names. The row was
    # created above with verbatim user_text; attach the metadata to it. Skip the
    # write when nothing was obfuscated so the column stays null (pre-feature /
    # no-PII rows are indistinguishable and both mean "show nothing").
    user_redactions = placeholder_redactions(redacted_text, getattr(tenant, "pii_entity_map", None))
    if user_redactions:
        AppChatMessage.objects.filter(pk=turn.pk).update(user_redactions=user_redactions)
        turn.user_redactions = user_redactions
    # A bare attachment with no caption still needs SOMETHING for the agent to
    # act on.
    if redacted_text:
        llm_text = redacted_text
    elif image:
        llm_text = "(the user sent a photo with no caption)"
    elif document:
        llm_text = "(the user sent a document with no caption)"
    else:
        llm_text = ""
    # Decorate like the other channels: current-time marker + the
    # "this is a chat turn, don't pre-load workspace docs" marker + any
    # attachment marker, then the user's (redacted) text.
    message_text = (
        build_datetime_context(user_tz) + build_chat_context_marker("ios") + image_marker + document_marker + llm_text
    )

    payload = {
        "message_text": message_text,
        "user_param": _thread_user_param(thread),
        "user_timezone": user_tz,
        "client_msg_id": client_msg_id,
        "thread_id": str(thread.id),
    }
    if image:
        # Force a singleton batch: a coalesced batch (cold-start burst) rebuilds
        # content from row.user_text, which carries no [Photo attached] marker —
        # so a coalesced image turn would lose the photo. Mirrors is_voice.
        payload["is_image"] = True
    if document:
        # Same singleton reasoning as is_image: the [Document attached] marker
        # lives only in message_text and would be dropped by a coalesced rebuild.
        payload["is_document"] = True

    enqueue_message_for_tenant(
        tenant=tenant,
        channel=PendingMessage.Channel.IOS,
        channel_user_id=str(thread.id),
        payload=payload,
        # REDACTED excerpt on the queue row: a coalesced batch rebuilds the
        # LLM-bound content from row.user_text (see pending_queue), so a raw
        # excerpt would leak unredacted PII to the model on a cold-start burst.
        # The verbatim text is preserved on AppChatMessage.user_text (persisted
        # above) which is what the ?since= display feed reads — mirrors the
        # Telegram poller, which also stores a redacted excerpt.
        user_text_excerpt=redacted_text,
    )
    ChatThread.objects.filter(id=thread.id).update(last_active_at=timezone.now())
    # iOS is a first-class channel for the idle-hibernation freshness signal:
    # without this stamp the sweep sees an iOS-only tenant as permanently idle
    # (canary sat 8 days stale) and check_cron_wake_idle's "did the user
    # message during this wake" test can never pass — so the container gets
    # hibernated out from under an active conversation. Mirrors the poller
    # channels' stamp in wake_on_message.py.
    Tenant.objects.filter(id=tenant.id).update(last_message_at=timezone.now())
    return turn, True


class ChatThreadListView(APIView):
    """GET: list the user's threads (ensures the main thread exists).
    POST: create a new named thread."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        _get_or_create_main_thread(tenant, request.user)
        threads = ChatThread.objects.filter(tenant=tenant)
        return _no_store(Response({"threads": [_serialize_thread(t, tenant=tenant) for t in threads]}))

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        title = scrub_chat_thread_title(str(request.data.get("title") or "").strip()[:120])
        thread = ChatThread.objects.create(
            tenant=tenant,
            user=request.user,
            title=title,
            title_enc=_encrypt_chat_value(tenant, enc_columns.CHAT_THREAD_TITLE, title),
            is_main=False,
        )
        return Response(_serialize_thread(thread, tenant=tenant), status=status.HTTP_201_CREATED)


class ChatThreadDetailView(APIView):
    """DELETE: remove a non-main thread and its app-message history."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, thread_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            thread = ChatThread.objects.select_for_update().filter(tenant=tenant, id=thread_id).first()
            if not thread:
                return Response({"error": "thread_not_found"}, status=status.HTTP_404_NOT_FOUND)
            if thread.is_main:
                return Response({"error": "cannot_delete_main"}, status=status.HTTP_409_CONFLICT)
            # AppChatMessage.thread is CASCADE, so this removes exactly this
            # thread's app-message rows in the same database transaction.
            thread.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class ChatThreadSetMainView(APIView):
    """POST: atomically make an existing tenant thread the main/Home thread."""

    permission_classes = [IsAuthenticated]

    def post(self, request, thread_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            # Lock every existing thread in a stable order. Every swap contends
            # on the current main row, serializing concurrent swaps without a
            # tenant-wide advisory lock or a schema change.
            threads = list(ChatThread.objects.select_for_update().filter(tenant=tenant).order_by("id"))
            thread = next((candidate for candidate in threads if candidate.id == thread_id), None)
            if thread is None:
                return Response({"error": "thread_not_found"}, status=status.HTTP_404_NOT_FOUND)
            changed = not thread.is_main
            if changed:
                current_main = next((candidate for candidate in threads if candidate.is_main), None)
                # The conditional unique index is immediate/non-deferrable.
                # Clear the old row first so setting the target can never
                # briefly create two is_main=True rows in this transaction.
                if current_main is not None:
                    ChatThread.objects.filter(pk=current_main.pk).update(is_main=False)
                ChatThread.objects.filter(pk=thread.pk).update(is_main=True)
                thread.is_main = True

        if changed:
            # Cache access stays outside the transaction (platform invariant 8).
            # Replace the old id only after the new main is committed.
            cache_main_thread_id(tenant.id, thread.id)
        return Response(_serialize_thread(thread, tenant=tenant), status=status.HTTP_200_OK)


class ChatThreadMessagesView(APIView):
    """GET: the recent turns in a thread (oldest→newest) for loading a
    conversation in the app."""

    permission_classes = [IsAuthenticated]

    def get(self, request, thread_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            thread = ChatThread.objects.filter(tenant=tenant, id=thread_id).first()
        except (ValueError, ValidationError):
            thread = None
        if not thread:
            return Response({"error": "thread_not_found"}, status=status.HTTP_404_NOT_FOUND)
        limit = _HISTORY_LIMIT
        # Newest N, returned oldest→newest so the app can append in order.
        rows = list(AppChatMessage.objects.filter(thread=thread).order_by("-created_at")[:limit])
        rows.reverse()
        # One map read for the whole page (rehydrating reply_text) — avoids a
        # per-row tenant FK lookup in _serialize_message.
        entity_map = getattr(tenant, "pii_entity_map", None)
        # Bulk-decrypt the whole page's user_text in ONE decrypt_bulk (one audit
        # event, row_count=N, owner_request) instead of N single reads — plan
        # amendment b. _serialize_message then just reveals the passed value.
        page_user_texts = enc_read.read_values_bulk(
            tenant,
            enc_columns.APP_CHAT_MESSAGE_USER_TEXT,
            [(m.user_text_enc, m.user_text) for m in rows],
        )
        return _no_store(
            Response(
                {
                    "thread": _serialize_thread(thread, tenant=tenant),
                    "messages": [
                        _serialize_message(m, entity_map=entity_map, user_text=ut)
                        for m, ut in zip(rows, page_user_texts)
                    ],
                }
            )
        )


class ChatMessageView(APIView):
    """POST: send a message → enqueue an OpenClaw turn through the tenant.
    GET:  the flat, cursor-paginated cross-channel history feed (``?since=``).

    POST returns immediately with the (pending) turn; the client polls
    ``ChatMessageDetailView`` for the reply. Idempotent on ``client_msg_id``.
    """

    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        # Throttle the SEND (POST) path only — mirrors the per-user hourly
        # throttles on the sibling chat endpoints. The GET ?since= feed is a
        # cheap poll clients hit every ~30s (and from multiple devices), so it
        # must stay unthrottled or steady-state polling would exhaust the budget.
        if getattr(self.request, "method", None) == "POST":
            return [ChatMessageSendHourThrottle()]
        return []

    def get(self, request):
        """Ascending cross-channel message history after an opaque cursor.

        ``?since=<cursor>`` (absent/empty = from the beginning); ``?limit=`` is
        clamped to the server bound. Returns ``{"messages": [...], "cursor":
        <next>}`` — see ``apps.router.chat_history`` for the row contract and
        cursor semantics. The cursor is replica-safe (a ``(created_at, id)``
        keyset), so any replica answers the same ``?since=`` identically.
        """
        from apps.router.chat_history import DEFAULT_PAGE_SIZE, build_since_page

        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        try:
            limit = int(request.query_params.get("limit", DEFAULT_PAGE_SIZE))
        except (TypeError, ValueError):
            limit = DEFAULT_PAGE_SIZE

        # Non-app channels (Telegram/LINE/cron) have no thread FK → map them to
        # the tenant's single shared main thread so iOS sees one rolling thread.
        # Cached: the id is immutable, so steady-state polls skip the lookup.
        main_thread_id = _main_thread_id_cached(tenant, request.user)
        messages, next_cursor = build_since_page(
            tenant,
            main_thread_id,
            cursor=request.query_params.get("since"),
            limit=limit,
        )
        return _no_store(Response({"messages": messages, "cursor": next_cursor}))

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        # Bound the raw body BEFORE DRF materializes it (the request.data access
        # below). DRF's JSONParser reads the request stream directly, bypassing
        # Django's DATA_UPLOAD_MAX_MEMORY_SIZE, so an unbounded base64 image body
        # could OOM the shared control plane. Content-Length is trusted only to
        # reject early; an absent/chunked length still falls back to Django's
        # capped ``.body`` when request.data is parsed.
        try:
            declared_len = int(request.META.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            declared_len = 0
        if declared_len > _MAX_REQUEST_BODY_BYTES:
            # Attachment-neutral: this coarse pre-body guard can't tell an image
            # from a PDF, and the body may now be either. (No deployed client
            # string-matches this code — verified across frontend + iOS.)
            return Response({"error": "request_too_large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        if not isinstance(request.data, dict):
            return Response({"error": "invalid_body"}, status=status.HTTP_400_BAD_REQUEST)

        # Idempotency FIRST: a retry carrying a known client_msg_id must replay
        # the existing turn (200) WITHOUT re-validating or re-storing its image,
        # so a photo is written to the share exactly once even if the client
        # resends a re-encoded body. This also precedes thread validation — a
        # now-stale/invalid thread_id on a retry must replay, not 404.
        client_msg_id = str(request.data.get("client_msg_id") or "").strip() or uuid.uuid4().hex
        if len(client_msg_id) > _CLIENT_MSG_ID_MAX:
            return Response({"error": "invalid_client_msg_id"}, status=status.HTTP_400_BAD_REQUEST)
        existing = AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).first()
        if existing:
            return Response(_serialize_message(existing), status=status.HTTP_200_OK)

        text = str(request.data.get("text") or "").strip()

        # Optional inbound image / PDF (base64 / data URL). Decode + validate up
        # front so a malformed payload is a 400 before any enqueue; the actual
        # share write is deferred to enqueue_tenant_turn (after the budget gate).
        image_bytes, image_ext, image_err = decode_and_validate_image(
            request.data.get("image"), max_bytes=MAX_APP_IMAGE_BYTES
        )
        if image_err:
            return Response({"error": image_err}, status=status.HTTP_400_BAD_REQUEST)

        document_bytes, document_ext, document_err = decode_and_validate_document(
            request.data.get("document"), max_bytes=MAX_APP_DOCUMENT_BYTES, tenant_id=str(tenant.id)
        )
        if document_err:
            return Response({"error": document_err}, status=status.HTTP_400_BAD_REQUEST)

        # One attachment per turn: attachment_path holds a single path, and a
        # turn carrying both a photo and a PDF has no clear single intent.
        if image_bytes is not None and document_bytes is not None:
            return Response({"error": "multiple_attachments"}, status=status.HTTP_400_BAD_REQUEST)

        # A bare attachment with no caption is a valid turn — require text OR an
        # image OR a document.
        if not text and image_bytes is None and document_bytes is None:
            return Response({"error": "empty_message"}, status=status.HTTP_400_BAD_REQUEST)
        if len(text) > _MAX_CHARS:
            return Response({"error": "message_too_long"}, status=status.HTTP_400_BAD_REQUEST)

        thread = _resolve_thread(request, tenant)
        if thread is None:
            return Response({"error": "thread_not_found"}, status=status.HTTP_404_NOT_FOUND)

        # Optional structured situation signal. Read only the label key: any
        # coordinate/accuracy-shaped siblings are deliberately ignored. The
        # idempotent replay return above runs before this seam, so a retried
        # client_msg_id cannot re-capture or re-push the observation.
        location = request.data.get("location")
        if isinstance(location, dict):
            from apps.orchestrator.envelope_registry import suppress_refresh
            from apps.tenants.situation import record_place_observation

            # UserSituation is registry-wired for out-of-band writes, but this
            # request owns the explicit background push below. Suppress the
            # model signal here so one accepted label change produces one push.
            with suppress_refresh():
                situation_changed = record_place_observation(
                    tenant,
                    location.get("place_label"),
                    "ios_chat",
                )
            if situation_changed:
                from apps.orchestrator.workspace_envelope import push_user_md_in_background

                push_user_md_in_background(tenant)

        turn, created = enqueue_tenant_turn(
            tenant=tenant,
            user=request.user,
            text=text,
            thread=thread,
            client_msg_id=client_msg_id,
            image=image_bytes,
            image_ext=image_ext or "jpg",
            document=document_bytes,
            document_ext=document_ext or "pdf",
        )
        http = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(_serialize_message(turn), status=http)


class ChatContextView(APIView):
    """GET: a compact markdown snapshot of the user's state for clients that
    run their own model (iOS private/on-device mode).

    Same per-pillar content as the USER.md managed region the tenant runtime
    bootstraps from — goals, tasks, fuel, finance, recent journal, the
    conversation digest — but size-capped for a small on-device context
    window. This is what makes the private mode assistant "know who you are"
    without any prompt text ever reaching a cloud model: the user's own data
    flows DOWN to the device; nothing flows out to a model provider.

    Unlike USER.md (consumed inside the tenant's placeholder-space pipeline),
    this digest is user-facing: the whole rendered snapshot has its PII
    placeholders rehydrated to real values (below) before it leaves — including
    the placeholder-space conversation digest that ``build_conversation_digest``
    now emits unrehydrated for the model-facing USER.md path.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ChatContextHourThrottle]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        from apps.orchestrator.workspace_envelope import (
            CONTEXT_DIGEST_DEFAULT_CHARS,
            CONTEXT_DIGEST_MAX_CHARS,
            CONTEXT_DIGEST_MIN_CHARS,
            CONTEXT_DIGEST_VERSION,
            render_context_digest,
        )

        try:
            max_chars = int(request.query_params.get("max_chars", CONTEXT_DIGEST_DEFAULT_CHARS))
        except (TypeError, ValueError):
            max_chars = CONTEXT_DIGEST_DEFAULT_CHARS
        max_chars = max(CONTEXT_DIGEST_MIN_CHARS, min(max_chars, CONTEXT_DIGEST_MAX_CHARS))

        context_md = render_context_digest(tenant, max_chars=max_chars, client_variant=True)

        # The device has no entity map, so a raw ``[PERSON_1]`` would be
        # parroted to the user verbatim. Fail-open: a rehydration error
        # serves placeholder-space text rather than no context at all.
        from apps.router.reply_text import finalize_outbound_text

        context_md = finalize_outbound_text(
            context_md,
            getattr(tenant, "pii_entity_map", None),
            tenant_id=tenant.id,
            channel="ios_context",
        )

        return _no_store(
            Response(
                {
                    "context_md": context_md,
                    "context_version": CONTEXT_DIGEST_VERSION,
                    "max_chars": max_chars,
                    "generated_at": timezone.now().isoformat(),
                }
            )
        )


class ChatLocalTurnView(APIView):
    """POST: record a turn that ALREADY happened on the client's own model
    (iOS private/on-device mode).

    The turn is stored as a READY ``AppChatMessage`` with
    ``source="on_device"`` so thread history, the USER.md "Conversation so
    far" digest, and nightly extraction all see it — the on-device assistant
    is a first-class channel, not a disconnected chatbot. Nothing is enqueued
    to the tenant container and no model budget is consumed: the reply was
    produced on-device, this is the after-the-fact record of it.

    INTENDED data flow (and the privacy copy must agree): the recorded text
    enters the USER.md conversation digest, which the tenant runtime — and
    therefore OpenRouter (zero-data-retention) — sees on later turns and
    crons. Private mode's promise is that INFERENCE for the turn stays on
    the device, not that the conversation is invisible to the user's own
    assistant afterwards; without this flow, crons and the other channels
    would be blind to on-device chats, which is the gap this endpoint closes.

    Idempotent on ``client_msg_id`` (clients retry from an offline outbox).
    ``occurred_at`` (optional, ISO 8601) backdates an outbox-delayed turn to
    when it actually happened, so the digest's "today" stays honest.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ChatLocalTurnHourThrottle]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        if not isinstance(request.data, dict):
            return Response({"error": "invalid_body"}, status=status.HTTP_400_BAD_REQUEST)

        # Truncate rather than reject: the record is an audit of a turn that
        # already happened — losing it entirely is worse than losing its
        # tail. (The enqueueing /messages/ endpoint rejects instead; its
        # bound protects the queue and the prompt, which don't exist here.)
        user_text = str(request.data.get("text") or "").strip()[:_MAX_CHARS]
        if not user_text:
            return Response({"error": "empty_message"}, status=status.HTTP_400_BAD_REQUEST)
        reply_text = clamp_reply_text(str(request.data.get("reply_text") or "").strip())

        client_msg_id = str(request.data.get("client_msg_id") or "").strip() or uuid.uuid4().hex
        if len(client_msg_id) > _CLIENT_MSG_ID_MAX:
            return Response({"error": "invalid_client_msg_id"}, status=status.HTTP_400_BAD_REQUEST)
        existing = AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).first()
        if existing:
            return Response(_serialize_message(existing), status=status.HTTP_200_OK)

        thread = _resolve_thread(request, tenant)
        if thread is None:
            return Response({"error": "thread_not_found"}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        occurred_at = _parse_occurred_at(request.data.get("occurred_at"))
        user_text_enc = _encrypt_chat_value(tenant, enc_columns.APP_CHAT_MESSAGE_USER_TEXT, user_text)
        try:
            turn = AppChatMessage.objects.create(
                tenant=tenant,
                user=request.user,
                thread=thread,
                client_msg_id=client_msg_id,
                user_text=user_text,
                user_text_enc=user_text_enc,
                reply_text=reply_text,
                status=AppChatMessage.Status.READY,
                source=AppChatMessage.Source.ON_DEVICE,
                replied_at=occurred_at or now,
            )
        except IntegrityError:
            # Concurrent outbox retry won the (tenant, client_msg_id) race.
            turn = AppChatMessage.objects.get(tenant=tenant, client_msg_id=client_msg_id)
            return Response(_serialize_message(turn), status=status.HTTP_200_OK)
        if occurred_at is not None:
            # created_at is auto_now_add (ignores supplied values), but the
            # conversation digest dates turns by it — backdate via update()
            # so an outbox-delayed turn lands on the day it happened.
            AppChatMessage.objects.filter(pk=turn.pk).update(created_at=occurred_at)
            turn.refresh_from_db()
        ChatThread.objects.filter(id=thread.id).update(last_active_at=now)

        # Same debounced USER.md push a captured Telegram/LINE turn triggers,
        # so the conversation digest reflects on-device chats before the next
        # cron fires.
        from apps.router.conversation_capture import schedule_user_md_refresh

        schedule_user_md_refresh(tenant)

        return Response(_serialize_message(turn), status=status.HTTP_201_CREATED)


class ChatMessageDetailView(APIView):
    """GET: poll a single turn for its reply/status."""

    permission_classes = [IsAuthenticated]

    def get(self, request, client_msg_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        turn = AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).first()
        if not turn:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return _no_store(Response(_serialize_message(turn)))


class ChatReadView(APIView):
    """POST: mark the in-app chat read up to now → clears the APNs unread badge.

    Stamps ``user.chat_last_read_at = now`` (the server read-cursor the badge
    count is computed against), so the NEXT alert push rides an absolute unread
    count of 0. The iOS app calls this when chat becomes visible, alongside
    clearing the local icon badge. Same JWT-authed consumer surface as
    ``ChatMessageView``. Returns ``{"unread": 0}`` (0 by definition right after a
    stamp).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        # Stamp via an UPDATE (not model.save) so we touch only this column — no
        # read-modify-write race against a concurrent write to other User fields.
        type(request.user).objects.filter(pk=request.user.pk).update(chat_last_read_at=timezone.now())
        return _no_store(Response({"unread": 0}, status=status.HTTP_200_OK))


class TranscriptionVocabView(APIView):
    """GET: the tenant's speech-to-text vocabulary for on-device recognition.

    iOS transcribes voice input ON DEVICE (Apple's speech recognizer) and POSTs
    text — the July 2026 "Rakuten"→"Rocketen" garble entered through that path,
    so the server never sees the audio and the server-side Whisper prompt hint
    cannot help this channel. The app fetches these terms and feeds them into
    ``SFSpeechRecognitionRequest.contextualStrings`` so known brands / projects /
    names decode with consistent spelling.

    Terms come from ``collect_transcription_vocab`` — pii_denylist keys,
    workspace names, the user's display name; never ``pii_entity_map`` contacts
    — with the same budget caps as the server-side Whisper hint, so every
    channel biases toward the identical vocabulary. Same JWT-authed consumer
    surface as ``ChatMessageView``. Returns ``{"terms": [...]}``.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.router.transcription import collect_transcription_vocab

        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        return _no_store(Response({"terms": collect_transcription_vocab(tenant)}))


_MAX_PARTIAL_TEXT_CHARS = 32000


def _parse_partial(data: dict) -> tuple[str | None, int | None]:
    """Parse the optional partial-text stream fields off a progress POST.

    Returns ``(text, seq)`` where both are non-None only when the post carries a
    valid partial: ``text`` is a string (truncated to 32k chars, matching the
    plugin's cap) and ``seq`` is a positive int. Any missing/malformed field
    yields ``(None, None)`` — the caller treats that as "no partial in this event".

    Strips a trailing (complete OR still-typing/unclosed) ``[[quick-replies:``
    or ``[[journal-link:`` marker at WRITE time — the same cumulative ``text``
    is persisted verbatim to ``AppChatMessage.partial_text`` and served
    unstripped while pending (``_serialize_message``), so without this the
    streaming bubble would flash the raw marker for however long it takes the
    terminal reply to land. Truncating first (so a boundary cut mid-marker still
    leaves a strippable trailing fragment), then stripping.
    """
    raw_text = data.get("text")
    if not isinstance(raw_text, str):
        return None, None
    try:
        seq = int(data.get("seq"))
    except (TypeError, ValueError):
        return None, None
    if seq <= 0:
        return None, None
    from apps.router.journal_link import strip_streaming_journal_link_marker
    from apps.router.quick_replies import strip_streaming_quick_reply_marker

    # Both are no-ops unless their own opener is the trailing fragment, and only
    # one marker can be the reply's final line, so chaining them is safe.
    text = strip_streaming_journal_link_marker(strip_streaming_quick_reply_marker(raw_text[:_MAX_PARTIAL_TEXT_CHARS]))
    return text, seq


class ChatProgressEventView(APIView):
    """POST (internal, container → control plane): narrate an in-flight turn.

    The per-tenant runtime's tool-call hooks report coarse progress here —
    ``waking`` → ``thinking`` → ``tool`` (+ a human detail like "searching your
    journal") → ``composing`` — so a polling client can show what the assistant
    is doing instead of an opaque spinner (and the iOS-27 Siri Live Activity can
    map it to ``progress.localizedDescription``).

    It also accepts optional per-step partial assistant text (``text`` + a
    monotonic ``seq``, from the nbhd-stream-progress plugin) for pseudo-streaming:
    the cumulative text-so-far is written to ``partial_text``/``partial_seq`` on
    the same resolved PENDING row, seq-guarded so an out-of-order/duplicate post
    can't rewind the stream. A post may carry a phase, a partial, or both; only a
    post with neither is rejected (400).

    Auth: ``X-NBHD-Internal-Key`` + ``X-NBHD-Tenant-Id`` (same internal-runtime
    auth as usage/gate callbacks). Best-effort narration: only a still-``pending``
    turn is updated; a missing/finished turn is a 200 no-op so a late event can
    never resurrect or mutate a completed turn.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request, tenant_id):
        from apps.integrations.internal_auth import (
            InternalAuthError,
            validate_internal_runtime_request,
        )

        try:
            validate_internal_runtime_request(
                provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
                provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
                expected_tenant_id=str(tenant_id),
            )
        except InternalAuthError as exc:
            return Response(
                {"error": "internal_auth_failed", "detail": str(exc)},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not isinstance(request.data, dict):
            return Response({"error": "invalid_body"}, status=status.HTTP_400_BAD_REQUEST)
        client_msg_id = str(request.data.get("client_msg_id") or "").strip()
        phase = str(request.data.get("phase") or "").strip()[:24]
        detail = str(request.data.get("detail") or "").strip()[:200]
        # Optional per-step partial assistant text (nbhd-stream-progress plugin).
        # A text-bearing post carries no phase; a phase-narration post carries no
        # text. Either is a valid, meaningful event — reject only a post with
        # NEITHER (preserves the empty-phase 400 for the phase-narration path).
        partial_text, partial_seq = _parse_partial(request.data)
        has_partial = partial_text is not None and partial_seq is not None
        if not phase and not has_partial:
            return Response({"error": "missing_fields"}, status=status.HTTP_400_BAD_REQUEST)

        base = AppChatMessage.objects.filter(tenant_id=tenant_id, status=AppChatMessage.Status.PENDING)
        # Whether the resolved row is a DETERMINISTIC match for the turn this
        # event belongs to. Partial assistant TEXT is attributed only when this
        # holds; the low-sensitivity phase spinner may still ride the best-effort
        # newest-PENDING fallback below.
        partial_attributable = True
        if client_msg_id:
            qs = base.filter(client_msg_id=client_msg_id)
        else:
            # The runtime tool-call hook didn't say which turn it is narrating
            # (no client_msg_id). Narrate the turn that is ACTUALLY in flight —
            # one whose thread currently holds a live drain lease
            # (PendingMessage.delivery_in_flight_until > now) — NOT merely the
            # newest PENDING row. iOS/Siri serialization is per-THREAD (one
            # OpenClaw session per ChatThread; PendingMessage.channel_user_id =
            # str(thread.id)), so several threads can have PENDING rows at once
            # while only the leased ones are being processed; a freshly-queued
            # turn on another thread is NOT in flight and must not steal the
            # spinner. The lease is held for the whole /v1/chat/completions turn
            # (lease = timeout × 1.5), exactly the window progress events fire in.
            # Among in-flight threads, narrate the oldest-started one's PENDING
            # rows (FIFO). Telegram/LINE create no AppChatMessage row → no-op.
            now = timezone.now()
            in_flight_thread_ids = list(
                PendingMessage.objects.filter(
                    tenant_id=tenant_id,
                    channel=PendingMessage.Channel.IOS,
                    delivery_status=PendingMessage.Status.PENDING,
                    delivery_in_flight_until__gt=now,
                ).values_list("channel_user_id", flat=True)
            )
            in_flight_oldest = (
                base.filter(thread_id__in=in_flight_thread_ids).order_by("created_at").first()
                if in_flight_thread_ids
                else None
            )
            if in_flight_oldest is not None:
                qs = base.filter(thread_id=in_flight_oldest.thread_id)
            else:
                # No live IOS lease matched (lease expired / narrow race) — fall
                # back to the newest PENDING row so a real PHASE event is never
                # silently dropped. But do NOT attribute partial reply TEXT here:
                # without a live IOS lease the turn ACTUALLY in flight may be a
                # Telegram/LINE turn (which creates no AppChatMessage row), and
                # writing its cumulative reply into an unrelated PENDING app row
                # would surface another channel's private reply as this turn's
                # streaming text. Phase-only on the fallback.
                latest_pk = base.order_by("-created_at").values_list("pk", flat=True).first()
                qs = base.filter(pk=latest_pk) if latest_pk is not None else base.none()
                partial_attributable = False
        # Phase narration overwrites in place (only when a phase was sent — a
        # text-only post must NOT clobber a live phase with an empty string).
        updated = 0
        if phase:
            updated += qs.update(phase=phase, phase_detail=detail)
        # Partial text is seq-guarded: only apply when this seq is strictly newer
        # than what's stored, so an out-of-order or duplicate post can't rewind
        # the stream. The atomic ``partial_seq__lt`` filter defeats the race
        # without a read-modify-write.
        if has_partial and partial_attributable:
            updated += qs.filter(partial_seq__lt=partial_seq).update(partial_text=partial_text, partial_seq=partial_seq)
        return Response({"updated": bool(updated)}, status=status.HTTP_200_OK)
