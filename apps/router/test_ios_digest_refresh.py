"""P1 regression: an iOS drain refreshes the USER.md conversation digest.

The golden-case root cause (docs/assistant-context-continuity-directive.md D1):
``record_conversation_turn`` — which fires the debounced ``push_user_md`` — is
called from the Telegram and LINE drains only, never from ``_drain_ios_batch``.
So an iOS-only user's isolated morning cron read an hours-stale digest.

These pin the fix at the seam it lives on: a HEALTHY iOS drain schedules the
same debounced refresh the other channels get (reusing
``conversation_capture.schedule_user_md_refresh`` — no parallel debounce), and a
drain that did NOT actually deliver (the OpenRouter credit-limit early return)
does not — a stale digest must never be pushed off a turn that didn't happen.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.router.models import PendingMessage
from apps.router.pending_queue import _drain_ios_batch
from apps.tenants.models import Tenant, User


def _ok_chat_response(text: str = "hi"):
    resp = MagicMock()
    resp.status_code = 200
    resp.is_success = True
    resp.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {},  # empty → _record_usage_safe is a no-op
        "model": "test",
    }
    resp.raise_for_status = MagicMock()
    return resp


class IOSDrainRefreshesDigestTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username=f"iosdigest_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            preferred_channel="telegram",
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-iosdigest.example.com",
        )

    def _pmsg(self) -> PendingMessage:
        # No client_msg_id → _store_ios_turn_reply is a no-op (needs no
        # AppChatMessage row); this test isolates the refresh seam, not reply
        # persistence (covered by test_ios_chat).
        return PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id="thread-abc",
            payload={"message_text": "hello", "user_param": "thread:thread-abc", "user_timezone": "UTC"},
            user_text="hello",
        )

    @patch("apps.router.pending_queue._store_ios_turn_reply")
    @patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="tok")
    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.router.conversation_capture.schedule_user_md_refresh")
    def test_successful_ios_drain_schedules_digest_refresh(self, mock_refresh, mock_post, _tok, _store):
        mock_post.return_value = _ok_chat_response("hi")

        delivered = _drain_ios_batch(self.tenant, [self._pmsg()], 30.0)

        self.assertTrue(delivered)
        mock_refresh.assert_called_once_with(self.tenant)

    @patch("apps.router.pending_queue._handle_openrouter_credit_limit")
    @patch("apps.router.pending_queue._store_ios_turn_error")
    @patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="tok")
    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.router.conversation_capture.schedule_user_md_refresh")
    def test_credit_limited_ios_drain_does_not_schedule_refresh(self, mock_refresh, mock_post, _tok, _err, _handle):
        # HTTP 402 is the canonical OpenRouter credit-limit signal → the drain
        # returns before storing a reply, so it must NOT refresh the digest.
        resp = MagicMock()
        resp.status_code = 402
        mock_post.return_value = resp

        delivered = _drain_ios_batch(self.tenant, [self._pmsg()], 30.0)

        self.assertFalse(delivered)
        mock_refresh.assert_not_called()
