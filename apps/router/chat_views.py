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
from django.db import IntegrityError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services import check_budget
from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import enqueue_message_for_tenant
from apps.router.services import build_chat_context_marker, build_datetime_context
from apps.tenants.throttling import ChatContextHourThrottle, ChatLocalTurnHourThrottle

logger = logging.getLogger(__name__)

# Upper bound on a single inbound chat message. Generous for a chat UI but
# bounded so a pathological payload can't bloat the queue row / prompt.
_MAX_CHARS = 8000

# Upper bound on a recorded on-device reply. On-device models are small and
# their replies short; anything longer is truncated rather than rejected so
# the audit record survives a pathological client.
_REPLY_MAX_CHARS = 16000

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


def _get_or_create_main_thread(tenant, user) -> ChatThread:
    """The shared default thread every channel resumes. One per tenant —
    the partial unique constraint makes the get_or_create race-safe."""
    thread, _ = ChatThread.objects.get_or_create(
        tenant=tenant,
        is_main=True,
        defaults={"user": user, "title": "Main"},
    )
    return thread


# A tenant's main-thread id is effectively immutable, so the read-heavy ?since=
# feed caches it rather than paying a get_or_create round trip to Sydney on every
# ~30s poll. The feed only needs the id (a label for non-app rows), not the
# object. Cache-aside: a miss (or any cache hiccup) falls straight through to the
# DB, and on the very first call still creates the thread. The cache is busted on
# is_main thread deletion via apps/router/signals.py, so a (rare) delete+recreate
# can't serve a dangling id past the next poll; the TTL is only a last-ditch bound.
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
    try:
        cache.set(key, tid, _MAIN_THREAD_ID_TTL)
    except Exception:  # noqa: BLE001
        pass
    return tid


def invalidate_main_thread_cache(tenant_id) -> None:
    """Drop the cached main-thread id for a tenant. Called on is_main thread
    deletion so a delete+recreate never serves the old, dangling id."""
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


def _serialize_thread(thread: ChatThread) -> dict:
    return {
        "id": str(thread.id),
        "title": thread.title,
        "is_main": thread.is_main,
        "created_at": thread.created_at.isoformat(),
        "last_active_at": thread.last_active_at.isoformat() if thread.last_active_at else None,
    }


def _serialize_message(msg: AppChatMessage) -> dict:
    return {
        "client_msg_id": msg.client_msg_id,
        "thread_id": str(msg.thread_id),
        "status": msg.status,
        "source": msg.source,
        "user_text": msg.user_text,
        "reply_text": msg.reply_text,
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
    }


def enqueue_tenant_turn(*, tenant, user, text: str, thread: ChatThread, client_msg_id: str):
    """Create a PENDING ``AppChatMessage`` and enqueue a Tier-3 OpenClaw turn.

    The single chokepoint for "route this ask to the full tenant agent": used by
    both the normal ``ChatMessageView`` POST and the Tier-2 fast-responder
    escalation path (``apps.router.siri_views``). Idempotent on
    ``client_msg_id`` and budget-gated, exactly once.

    Returns ``(turn, created)`` — ``created`` is True only when a fresh PENDING
    turn was enqueued (so the caller can pick 201 vs 200). A budget-exhausted
    turn is recorded as ERROR and returned with ``created=False`` (nothing
    enqueued, no container woken).
    """
    existing = AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).first()
    if existing:
        return existing, False

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
            status=AppChatMessage.Status.PENDING,
        )
    except IntegrityError:
        # Concurrent retry won the (tenant, client_msg_id) race; the winner
        # already enqueued the tenant turn — replay, don't re-enqueue.
        turn = AppChatMessage.objects.get(tenant=tenant, client_msg_id=client_msg_id)
        return turn, False

    user_tz = getattr(user, "timezone", None) or "UTC"
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
    # Decorate like the other channels: current-time marker + the
    # "this is a chat turn, don't pre-load workspace docs" marker.
    message_text = build_datetime_context(user_tz) + build_chat_context_marker() + redacted_text

    enqueue_message_for_tenant(
        tenant=tenant,
        channel=PendingMessage.Channel.IOS,
        channel_user_id=str(thread.id),
        payload={
            "message_text": message_text,
            "user_param": _thread_user_param(thread),
            "user_timezone": user_tz,
            "client_msg_id": client_msg_id,
            "thread_id": str(thread.id),
        },
        user_text_excerpt=text,
    )
    ChatThread.objects.filter(id=thread.id).update(last_active_at=timezone.now())
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
        return _no_store(Response({"threads": [_serialize_thread(t) for t in threads]}))

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        title = str(request.data.get("title") or "").strip()[:120]
        thread = ChatThread.objects.create(
            tenant=tenant,
            user=request.user,
            title=title,
            is_main=False,
        )
        return Response(_serialize_thread(thread), status=status.HTTP_201_CREATED)


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
        return _no_store(
            Response(
                {
                    "thread": _serialize_thread(thread),
                    "messages": [_serialize_message(m) for m in rows],
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
        if not isinstance(request.data, dict):
            return Response({"error": "invalid_body"}, status=status.HTTP_400_BAD_REQUEST)

        text = str(request.data.get("text") or "").strip()
        if not text:
            return Response({"error": "empty_message"}, status=status.HTTP_400_BAD_REQUEST)
        if len(text) > _MAX_CHARS:
            return Response({"error": "message_too_long"}, status=status.HTTP_400_BAD_REQUEST)

        # Idempotency: a client-supplied stable id makes retries safe.
        client_msg_id = str(request.data.get("client_msg_id") or "").strip() or uuid.uuid4().hex
        if len(client_msg_id) > _CLIENT_MSG_ID_MAX:
            return Response({"error": "invalid_client_msg_id"}, status=status.HTTP_400_BAD_REQUEST)

        # Idempotency MUST precede thread validation: a retry carrying the same
        # client_msg_id but a now-stale/invalid thread_id must replay the existing
        # turn (200), not 404. (enqueue_tenant_turn re-checks too, but only after
        # a thread is resolved — so the early replay has to live here.)
        existing = AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).first()
        if existing:
            return Response(_serialize_message(existing), status=status.HTTP_200_OK)

        thread = _resolve_thread(request, tenant)
        if thread is None:
            return Response({"error": "thread_not_found"}, status=status.HTTP_404_NOT_FOUND)

        turn, created = enqueue_tenant_turn(
            tenant=tenant,
            user=request.user,
            text=text,
            thread=thread,
            client_msg_id=client_msg_id,
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
    this digest is user-facing: PII placeholders are rehydrated to real
    values before it leaves, mirroring ``clean_reply_for_capture``.
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
            render_context_digest,
        )

        try:
            max_chars = int(request.query_params.get("max_chars", CONTEXT_DIGEST_DEFAULT_CHARS))
        except (TypeError, ValueError):
            max_chars = CONTEXT_DIGEST_DEFAULT_CHARS
        max_chars = max(CONTEXT_DIGEST_MIN_CHARS, min(max_chars, CONTEXT_DIGEST_MAX_CHARS))

        context_md = render_context_digest(tenant, max_chars=max_chars)

        # The device has no entity map, so a raw ``[PERSON_1]`` would be
        # parroted to the user verbatim. Fail-open: a rehydration error
        # serves placeholder-space text rather than no context at all.
        entity_map = getattr(tenant, "pii_entity_map", None)
        if entity_map:
            try:
                from apps.pii.redactor import rehydrate_text

                context_md = rehydrate_text(context_md, entity_map)
            except Exception:
                logger.exception("chat context: PII rehydrate failed (non-fatal)")

        return _no_store(
            Response(
                {
                    "context_md": context_md,
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
        reply_text = str(request.data.get("reply_text") or "").strip()[:_REPLY_MAX_CHARS]

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
        try:
            turn = AppChatMessage.objects.create(
                tenant=tenant,
                user=request.user,
                thread=thread,
                client_msg_id=client_msg_id,
                user_text=user_text,
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


class ChatProgressEventView(APIView):
    """POST (internal, container → control plane): narrate an in-flight turn.

    The per-tenant runtime's tool-call hooks report coarse progress here —
    ``waking`` → ``thinking`` → ``tool`` (+ a human detail like "searching your
    journal") → ``composing`` — so a polling client can show what the assistant
    is doing instead of an opaque spinner (and the iOS-27 Siri Live Activity can
    map it to ``progress.localizedDescription``).

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
        if not phase:
            return Response({"error": "missing_fields"}, status=status.HTTP_400_BAD_REQUEST)

        base = AppChatMessage.objects.filter(tenant_id=tenant_id, status=AppChatMessage.Status.PENDING)
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
                # No live lease matched (lease expired / narrow race) — fall back
                # to the newest PENDING row so a real progress event is never
                # silently dropped.
                latest_pk = base.order_by("-created_at").values_list("pk", flat=True).first()
                qs = base.filter(pk=latest_pk) if latest_pk is not None else base.none()
        updated = qs.update(phase=phase, phase_detail=detail)
        return Response({"updated": bool(updated)}, status=status.HTTP_200_OK)
