"""APNs device-token registration + the reply-ready push helper.

``PushRegisterView`` (JWT-authed) lets the iOS app register/refresh/unregister
its APNs device token. ``notify_app_reply_ready`` is called from the iOS drain
when a turn's reply lands, to push "your answer is ready" — closing the
fire-and-forget gap (see ``HER_SIRI_ARCHITECTURE.md``). Both are no-ops in
substance until APNs is provisioned (see ``apps.common.apns``).
"""

from __future__ import annotations

import logging
import re

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.router.models import DeviceToken
from apps.tenants.throttling import PushTestMinuteThrottle

logger = logging.getLogger(__name__)

# APNs tokens are hex. 32 bytes today (64 chars); bound generously but reject
# obvious garbage so we don't store junk that can never receive a push.
_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")

# Preview length for the push body — a glanceable taste of the reply.
_PREVIEW_CHARS = 140

# A markdown table separator row (e.g. ``| --- | --- |``) — the reliable marker
# that a reply contains a table, which has no readable one-line plain-text form.
_TABLE_SEP_RE = re.compile(r"(?m)^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$")

# Content-free fallback when a reply has no good glanceable preview (a table, or
# empty text) — keeps structured / sensitive content off the lock screen.
_GENERIC_BODY = "Your reply is ready — tap to read."

# Content-free body for a turn that ended in error (budget exhausted, empty
# model reply). Never carries the machine reason — nothing diagnostic on the
# lock screen.
_ERROR_BODY = "Your assistant couldn't finish that — tap to try again."


def _markdown_to_plain_prose(text: str) -> str:
    """Strip common markdown to a single readable line — no visible syntax, no
    box-drawing. For prose replies only; tables are handled upstream."""
    s = text
    s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)  # fenced code blocks
    s = re.sub(r"`([^`]*)`", r"\1", s)  # inline code
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)  # images
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)  # links → text
    s = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", s)  # ATX headings
    s = re.sub(r"(?m)^\s*>\s?", "", s)  # blockquotes
    s = re.sub(r"(?m)^\s*[-*+]\s+", "", s)  # bullet markers
    s = re.sub(r"(?m)^\s*\d+\.\s+", "", s)  # ordered-list markers
    s = re.sub(r"(?m)^\s*([-*_])\1{2,}\s*$", " ", s)  # horizontal rules
    s = re.sub(r"(\*\*|__)(.*?)\1", r"\2", s)  # bold
    s = re.sub(r"(\*|_)(.*?)\1", r"\2", s)  # italic
    s = s.replace("*", "").replace("`", "")  # stray markers
    return " ".join(s.split())  # collapse to one line


def _notification_body(reply_text: str | None) -> str:
    """Plain-text push body for a reply (APNs alert text is plain text only).

    Hybrid per Apple HIG: a short, markdown-stripped one-line *taste* for simple
    prose replies, but a generic content-free line for tables (no readable
    one-line form) or empty replies — keeping structured / sensitive content off
    the lock screen and out of Announce.
    """
    text = reply_text or ""
    if not text.strip() or _TABLE_SEP_RE.search(text):
        return _GENERIC_BODY
    plain = _markdown_to_plain_prose(text)
    if not plain:
        return _GENERIC_BODY
    if len(plain) > _PREVIEW_CHARS:
        plain = plain[: _PREVIEW_CHARS - 1].rstrip() + "…"
    return plain


class PushRegisterView(APIView):
    """POST: register/refresh this install's APNs token. DELETE: unregister it."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        if not isinstance(request.data, dict):
            return Response({"error": "invalid_body"}, status=status.HTTP_400_BAD_REQUEST)

        token = str(request.data.get("device_token") or "").strip()
        if not _TOKEN_RE.match(token):
            return Response({"error": "invalid_token"}, status=status.HTTP_400_BAD_REQUEST)

        environment = str(request.data.get("environment") or DeviceToken.Environment.PRODUCTION).strip().lower()
        if environment not in DeviceToken.Environment.values:
            environment = DeviceToken.Environment.PRODUCTION
        bundle_id = str(request.data.get("bundle_id") or "").strip()[:128]

        # ``token`` is globally unique, so a single upsert re-points the device to
        # the registering user (account switch / device handoff on the same
        # install) atomically — no cross-user delete, no delete+create race.
        # update_or_create absorbs the same-token concurrent retry via the unique
        # constraint (it re-fetches on IntegrityError).
        DeviceToken.objects.update_or_create(
            token=token,
            defaults={"user": request.user, "tenant": tenant, "environment": environment, "bundle_id": bundle_id},
        )
        return Response({"registered": True}, status=status.HTTP_200_OK)

    def delete(self, request):
        # Token may ride a query param (clients whose DELETE carries no body) or
        # the request body.
        token = str(request.query_params.get("device_token") or "").strip()
        if not token and isinstance(request.data, dict):
            token = str(request.data.get("device_token") or "").strip()
        if not token:
            return Response({"error": "invalid_token"}, status=status.HTTP_400_BAD_REQUEST)
        DeviceToken.objects.filter(user=request.user, token=token).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PushTestView(APIView):
    """POST: send a static, no-PII test push to the CALLER'S OWN device(s).

    A self-service "is delivery working?" probe for confirming on-device
    notification delivery end to end (APNs ``200`` means Apple accepted it, not
    that the device displayed it — this lets a user verify the last hop).
    Security properties:

    * JWT-authed, and it operates ONLY on ``request.user``'s registered tokens —
      it never accepts a target token from the request, so it cannot push to any
      other device.
    * The push body is a FIXED string: no reply text, journal, finance, name, or
      any user/tenant data is ever placed in the payload.
    * The response carries counts only — never the token or any PII.
    * Rate-limited so it can't be used to hammer APNs or spam a device.

    Routes each device to the APNs host matching its stored ``environment`` and
    prunes any token APNs reports as unregistered (410), exactly like the
    reply-ready path, so a stale token self-heals (the app re-registers on next
    launch).
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [PushTestMinuteThrottle]

    def post(self, request):
        from apps.common.apns import apns_configured, send_push

        if not apns_configured():
            return Response({"sent": 0, "failed": 0, "skipped": "not_configured"}, status=status.HTTP_200_OK)

        rows = list(DeviceToken.objects.filter(user=request.user).values("token", "environment"))
        if not rows:
            return Response({"sent": 0, "failed": 0, "skipped": "no_tokens"}, status=status.HTTP_200_OK)

        # Route each device to the APNs host matching its environment (a sandbox
        # Debug-build token and a production App Store token can coexist).
        by_env: dict[str, list[str]] = {}
        for r in rows:
            by_env.setdefault(r["environment"], []).append(r["token"])

        sent = failed = 0
        stale: list[str] = []
        for env_name, env_tokens in by_env.items():
            res = send_push(
                env_tokens,
                title="NBHD",
                body="Test push — notifications are working.",  # static; no user data
                sandbox=(env_name == DeviceToken.Environment.SANDBOX),
            )
            sent += res.get("sent", 0)
            failed += res.get("failed", 0)
            stale.extend(res.get("unregistered") or [])
        if stale:
            DeviceToken.objects.filter(user=request.user, token__in=stale).delete()

        return Response(
            {"sent": sent, "failed": failed, "unregistered": len(stale)},
            status=status.HTTP_200_OK,
        )


def _notify_turn(tenant, client_msg_ids, *, body: str, content_available: bool) -> None:
    """Push one alert for a just-completed iOS turn, exactly once. Fail-open.

    Short-circuits before any DB work when APNs isn't configured (the common
    case today). Idempotency: an atomic ``notified_at`` claim guarantees a single
    push per turn even if the drain re-runs (a QStash retry, a re-leased batch) —
    ``apns-collapse-id`` is the device-side belt, this claim is the server-side
    suspenders. Routes each device to the APNs host matching its stored
    ``environment`` (a wrong-host send fails silently) and prunes tokens APNs
    reports as unregistered so the table self-heals. The iOS app is expected to
    suppress the alert when the relevant thread is already foregrounded
    (UNUserNotificationCenterDelegate).
    """
    from apps.common.apns import apns_configured

    if not apns_configured():
        return
    client_msg_ids = [c for c in (client_msg_ids or []) if c]
    if not client_msg_ids:
        return
    try:
        from apps.router.models import AppChatMessage

        # Atomic claim: only the first delivery to reach these turn(s) pushes.
        # ``notified_at__isnull`` makes a re-drain a no-op (rowcount 0).
        claimed = AppChatMessage.objects.filter(
            tenant=tenant, client_msg_id__in=client_msg_ids, notified_at__isnull=True
        ).update(notified_at=timezone.now())
        if not claimed:
            return

        msg = (
            AppChatMessage.objects.filter(tenant=tenant, client_msg_id__in=client_msg_ids)
            .select_related("user")
            .first()
        )
        if msg is None:
            return
        _push_to_user_devices(
            msg.user,
            body=body,
            thread_id=str(msg.thread_id),
            collapse_id=msg.client_msg_id,
            content_available=content_available,
            extra={"client_msg_id": msg.client_msg_id},
        )
    except Exception:
        logger.warning("push: notify turn failed (non-fatal)", exc_info=True)


def _push_to_user_devices(
    user,
    *,
    body: str,
    thread_id: str | None,
    collapse_id: str | None,
    content_available: bool,
    extra: dict,
) -> None:
    """Send one ``body`` alert to each of ``user``'s registered devices.

    Routes each device to the APNs host matching its stored ``environment`` — a
    sandbox (Debug-build) token and a production (App Store) token can coexist
    for one user, and a token sent to the wrong host fails with BadDeviceToken —
    and prunes any token APNs reports as unregistered (410) so the table
    self-heals. Shared by the app-turn (``_notify_turn``) and cron / proactive
    (``notify_proactive_ready``) push paths so the per-environment fan-out and
    self-healing live in exactly one place.
    """
    from apps.common.apns import send_push

    rows = list(DeviceToken.objects.filter(user=user).values("token", "environment"))
    if not rows:
        return

    by_env: dict[str, list[str]] = {}
    for r in rows:
        by_env.setdefault(r["environment"], []).append(r["token"])

    stale: list[str] = []
    for env_name, env_tokens in by_env.items():
        res = send_push(
            env_tokens,
            title="NBHD",
            body=body,
            sandbox=(env_name == DeviceToken.Environment.SANDBOX),
            thread_id=thread_id,
            collapse_id=collapse_id,
            content_available=content_available,
            extra=extra,
        )
        stale.extend(res.get("unregistered") or [])
    if stale:
        DeviceToken.objects.filter(user=user, token__in=stale).delete()


def notify_app_reply_ready(tenant, client_msg_ids, reply_text: str | None) -> None:
    """Push 'your answer is ready' for a just-completed iOS turn (hybrid push)."""
    _notify_turn(tenant, client_msg_ids, body=_notification_body(reply_text), content_available=True)


def notify_app_reply_error(tenant, client_msg_ids) -> None:
    """Push a generic 'couldn't finish' alert for a turn that ended in error
    (budget exhausted, empty model reply). Content-free body — no reason on the
    lock screen. Same idempotent, per-environment, self-healing path as the
    reply-ready push."""
    _notify_turn(tenant, client_msg_ids, body=_ERROR_BODY, content_available=True)


def notify_proactive_ready(tenant, proactive_id, body_source: str | None) -> None:
    """Push 'new message' for a just-delivered cron / proactive send, once.

    The iOS counterpart to ``notify_app_reply_ready`` for messages the user did
    NOT initiate from the app — a cron check-in, a meditation-ready ping, any
    ``nbhd_send_to_user`` proactive push. Such a send has no ``AppChatMessage``
    and no ``client_msg_id``, so idempotency rides ``ProactiveOutbound.notified_at``
    (the same atomic isnull→now claim) and the recipient is resolved from
    ``tenant.user`` (a ``ProactiveOutbound`` carries only a channel id, not a User).

    The push is only a wake-and-sync trigger: its body is a glanceable, PII-safe
    taste; the authoritative text reaches the app via the ``GET /chat/messages/
    ?since=`` feed (the ``cron:<id>`` row). Short-circuits before any DB work when
    APNs is unconfigured (the common case today). Fail-open.
    """
    from apps.common.apns import apns_configured

    if not apns_configured():
        return
    if not proactive_id:
        return
    try:
        from apps.router.models import ChatThread, ProactiveOutbound

        # Atomic claim: only the first push for this row wins. A re-run for the
        # same row (a future retry / reconcile path) is a no-op (rowcount 0).
        claimed = ProactiveOutbound.objects.filter(id=proactive_id, notified_at__isnull=True).update(
            notified_at=timezone.now()
        )
        if not claimed:
            return

        user = getattr(tenant, "user", None)
        if user is None:
            return

        # The cron row maps to the tenant's shared main thread in the ?since= feed
        # (chat_history._proactive_rows); mirror that thread-id here so a future
        # thread-aware client routes the alert to the same place. iOS ignores
        # thread-id today, so a tenant without a main thread (None) is fine.
        main_thread_id = ChatThread.objects.filter(tenant=tenant, is_main=True).values_list("id", flat=True).first()

        collapse = f"cron:{proactive_id}"
        _push_to_user_devices(
            user,
            body=_notification_body(body_source),
            thread_id=str(main_thread_id) if main_thread_id else None,
            collapse_id=collapse,
            content_available=True,
            extra={"id": collapse, "source": "cron"},
        )
    except Exception:
        logger.warning("push: notify proactive failed (non-fatal)", exc_info=True)
