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

from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services import check_budget
from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import enqueue_message_for_tenant
from apps.router.services import build_chat_context_marker, build_datetime_context

logger = logging.getLogger(__name__)

# Upper bound on a single inbound chat message. Generous for a chat UI but
# bounded so a pathological payload can't bloat the queue row / prompt.
_MAX_CHARS = 8000

# Upper bound on a recorded on-device reply. On-device models are small and
# their replies short; anything longer is truncated rather than rejected so
# the audit record survives a pathological client.
_REPLY_MAX_CHARS = 16000

# Context-digest size bounds (chars). The default suits Apple's on-device
# foundation model (~4k-token window shared with tool schemas + transcript);
# clients can ask for less or more within the clamp.
_CONTEXT_DEFAULT_CHARS = 6000
_CONTEXT_MIN_CHARS = 1000
_CONTEXT_MAX_CHARS = 16000

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
    }


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

    Returns immediately with the (pending) turn; the client polls
    ``ChatMessageDetailView`` for the reply. Idempotent on ``client_msg_id``.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        text = str(request.data.get("text") or "").strip()
        if not text:
            return Response({"error": "empty_message"}, status=status.HTTP_400_BAD_REQUEST)
        if len(text) > _MAX_CHARS:
            return Response({"error": "message_too_long"}, status=status.HTTP_400_BAD_REQUEST)

        # Idempotency: a client-supplied stable id makes retries safe.
        client_msg_id = str(request.data.get("client_msg_id") or "").strip() or uuid.uuid4().hex
        existing = AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).first()
        if existing:
            return Response(_serialize_message(existing), status=status.HTTP_200_OK)

        thread = _resolve_thread(request, tenant)
        if thread is None:
            return Response({"error": "thread_not_found"}, status=status.HTTP_404_NOT_FOUND)

        # Budget gate — mirror the webhook: don't enqueue work (or wake a
        # container) for an over-budget tenant. The turn is recorded as an
        # error so the client surfaces the reason instead of polling forever.
        budget_reason = check_budget(tenant)
        if budget_reason:
            turn = AppChatMessage.objects.create(
                tenant=tenant,
                user=request.user,
                thread=thread,
                client_msg_id=client_msg_id,
                user_text=text,
                status=AppChatMessage.Status.ERROR,
                error="budget_exhausted",
                replied_at=timezone.now(),
            )
            return Response(_serialize_message(turn), status=status.HTTP_200_OK)

        turn = AppChatMessage.objects.create(
            tenant=tenant,
            user=request.user,
            thread=thread,
            client_msg_id=client_msg_id,
            user_text=text,
            status=AppChatMessage.Status.PENDING,
        )

        user_tz = getattr(request.user, "timezone", None) or "UTC"
        # Decorate like the other channels: current-time marker + the
        # "this is a chat turn, don't pre-load workspace docs" marker.
        message_text = build_datetime_context(user_tz) + build_chat_context_marker() + text

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

        return Response(_serialize_message(turn), status=status.HTTP_201_CREATED)


class ChatContextView(APIView):
    """GET: a compact markdown snapshot of the user's state for clients that
    run their own model (iOS private/on-device mode).

    Same per-pillar content as the USER.md managed region the tenant runtime
    bootstraps from — goals, tasks, fuel, finance, recent journal, the
    conversation digest — but size-capped for a small on-device context
    window. This is what makes the private mode assistant "know who you are"
    without any prompt text ever reaching a cloud model: the user's own data
    flows DOWN to the device; nothing flows out to a model provider.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        try:
            max_chars = int(request.query_params.get("max_chars", _CONTEXT_DEFAULT_CHARS))
        except (TypeError, ValueError):
            max_chars = _CONTEXT_DEFAULT_CHARS
        max_chars = max(_CONTEXT_MIN_CHARS, min(max_chars, _CONTEXT_MAX_CHARS))

        from apps.orchestrator.workspace_envelope import render_context_digest

        context_md = render_context_digest(tenant, max_chars=max_chars)
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

    Idempotent on ``client_msg_id`` (clients retry from an offline outbox).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        user_text = str(request.data.get("text") or "").strip()
        if not user_text:
            return Response({"error": "empty_message"}, status=status.HTTP_400_BAD_REQUEST)
        if len(user_text) > _MAX_CHARS:
            return Response({"error": "message_too_long"}, status=status.HTTP_400_BAD_REQUEST)

        # Truncate rather than reject: the record is an audit of a turn that
        # already happened — losing it entirely is worse than losing its tail.
        reply_text = str(request.data.get("reply_text") or "").strip()[:_REPLY_MAX_CHARS]

        client_msg_id = str(request.data.get("client_msg_id") or "").strip() or uuid.uuid4().hex
        existing = AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).first()
        if existing:
            return Response(_serialize_message(existing), status=status.HTTP_200_OK)

        thread = _resolve_thread(request, tenant)
        if thread is None:
            return Response({"error": "thread_not_found"}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        turn = AppChatMessage.objects.create(
            tenant=tenant,
            user=request.user,
            thread=thread,
            client_msg_id=client_msg_id,
            user_text=user_text,
            reply_text=reply_text,
            status=AppChatMessage.Status.READY,
            source=AppChatMessage.Source.ON_DEVICE,
            replied_at=now,
        )
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
