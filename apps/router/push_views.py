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


def notify_app_reply_ready(tenant, client_msg_ids, reply_text: str | None) -> None:
    """Push 'your answer is ready' for a just-completed iOS turn. Fail-open.

    Short-circuits before any DB work when APNs isn't configured (the common
    case today). Prunes tokens APNs reports as unregistered so the table
    self-heals. The iOS app is expected to suppress the alert when the relevant
    thread is already foregrounded (UNUserNotificationCenterDelegate).
    """
    from apps.common.apns import apns_configured, send_push

    if not apns_configured():
        return
    client_msg_ids = [c for c in (client_msg_ids or []) if c]
    if not client_msg_ids:
        return
    try:
        from apps.router.models import AppChatMessage

        msg = (
            AppChatMessage.objects.filter(tenant=tenant, client_msg_id__in=client_msg_ids)
            .select_related("user")
            .first()
        )
        if msg is None:
            return
        rows = list(DeviceToken.objects.filter(user=msg.user).values("token", "environment"))
        if not rows:
            return
        preview = _notification_body(reply_text)

        # Route each device to the APNs host matching its environment — a sandbox
        # (Debug-build) token and a production (App Store) token can coexist for one
        # user, and a token sent to the wrong host fails with BadDeviceToken.
        by_env: dict[str, list[str]] = {}
        for r in rows:
            by_env.setdefault(r["environment"], []).append(r["token"])

        stale: list[str] = []
        for env_name, env_tokens in by_env.items():
            res = send_push(
                env_tokens,
                title="NBHD",
                body=preview,
                sandbox=(env_name == DeviceToken.Environment.SANDBOX),
                thread_id=str(msg.thread_id),
                extra={"client_msg_id": msg.client_msg_id},
            )
            stale.extend(res.get("unregistered") or [])
        if stale:
            DeviceToken.objects.filter(user=msg.user, token__in=stale).delete()
    except Exception:
        logger.warning("push: notify_app_reply_ready failed (non-fatal)", exc_info=True)
