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

logger = logging.getLogger(__name__)

# APNs tokens are hex. 32 bytes today (64 chars); bound generously but reject
# obvious garbage so we don't store junk that can never receive a push.
_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")

# Preview length for the push body — a glanceable taste of the reply.
_PREVIEW_CHARS = 140


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
        preview = " ".join((reply_text or "").split())
        if len(preview) > _PREVIEW_CHARS:
            preview = preview[: _PREVIEW_CHARS - 1].rstrip() + "…"

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
                body=preview or "Your assistant replied.",
                sandbox=(env_name == DeviceToken.Environment.SANDBOX),
                thread_id=str(msg.thread_id),
                extra={"client_msg_id": msg.client_msg_id},
            )
            stale.extend(res.get("unregistered") or [])
        if stale:
            DeviceToken.objects.filter(user=msg.user, token__in=stale).delete()
    except Exception:
        logger.warning("push: notify_app_reply_ready failed (non-fatal)", exc_info=True)
