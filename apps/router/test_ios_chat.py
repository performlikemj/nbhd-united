"""Tests for the iOS/rich-client chat ingress (route chat through tenant).

The iOS app becomes a channel into the tenant's OpenClaw runtime — same
USER.md/memory as Telegram/LINE — but with no push transport, so the reply
is persisted to ``AppChatMessage`` and the client polls for it. These tests
cover the additive PR1 slice:

  - POST a message → routes through the tenant (``thread:<id>`` user param,
    X-Channel ios) → reply persisted → poll returns it
  - idempotency on ``client_msg_id``
  - the shared "main" thread is the default and reused across messages
  - named threads get their own ``user_param`` (own OpenClaw session)
  - the budget gate blocks enqueue for an over-budget tenant

The drain runs inline on publish in the test path (see
``test_pending_queue.py``), so a POST drives the OC turn through to a
persisted reply within the request.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.tenants.models import Tenant, User


def _make_user() -> User:
    return User.objects.create_user(
        username=f"ios_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        preferred_channel="telegram",
    )


def _make_tenant(user: User) -> Tenant:
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-ios.example.com",
    )


def _ok_chat_response(text: str = "ok"):
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


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSChatRoutingTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.router.pending_queue.httpx.post")
    def test_message_routes_through_tenant_and_persists_reply(self, mock_post):
        mock_post.return_value = _ok_chat_response("Of course I know you, MJ.")

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "who am I?", "client_msg_id": "c1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["status"], "pending")
        self.assertEqual(resp.data["client_msg_id"], "c1")

        # A PendingMessage was enqueued on the ios channel with a thread-scoped
        # user param (NOT a channel id) and the client_msg_id on its payload.
        pmsg = PendingMessage.objects.get(tenant=self.tenant, channel=PendingMessage.Channel.IOS)
        main = ChatThread.objects.get(tenant=self.tenant, is_main=True)
        self.assertEqual(pmsg.channel_user_id, str(main.id))
        self.assertEqual(pmsg.payload["user_param"], f"thread:{main.id}")
        self.assertEqual(pmsg.payload["client_msg_id"], "c1")

        # The gateway POST carried the thread user param + the ios channel header.
        sent = mock_post.call_args.kwargs
        self.assertEqual(sent["json"]["user"], f"thread:{main.id}")
        self.assertEqual(sent["headers"]["X-Channel"], "ios")

        # The reply was persisted; the client polls and gets it.
        poll = self.client.get("/api/v1/chat/messages/c1/")
        self.assertEqual(poll.status_code, 200, poll.content)
        self.assertEqual(poll.data["status"], "ready")
        self.assertIn("I know you", poll.data["reply_text"])

    @patch("apps.router.pending_queue.httpx.post")
    def test_idempotent_on_client_msg_id(self, mock_post):
        mock_post.return_value = _ok_chat_response("hi")

        first = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "hello", "client_msg_id": "dup"},
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)

        second = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "hello again", "client_msg_id": "dup"},
            format="json",
        )
        # Second is a no-op replay → 200, returns the existing turn.
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(
            AppChatMessage.objects.filter(tenant=self.tenant, client_msg_id="dup").count(),
            1,
        )

    @patch("apps.router.pending_queue.httpx.post")
    def test_main_thread_is_default_and_shared(self, mock_post):
        mock_post.return_value = _ok_chat_response("ok")

        self.client.post("/api/v1/chat/messages/", {"text": "one", "client_msg_id": "a"}, format="json")
        self.client.post("/api/v1/chat/messages/", {"text": "two", "client_msg_id": "b"}, format="json")

        # Both default to the single shared main thread.
        self.assertEqual(ChatThread.objects.filter(tenant=self.tenant, is_main=True).count(), 1)
        params = {p.payload["user_param"] for p in PendingMessage.objects.filter(tenant=self.tenant)}
        self.assertEqual(len(params), 1)  # same thread → same user_param

    @patch("apps.router.pending_queue.httpx.post")
    def test_named_thread_has_own_session(self, mock_post):
        mock_post.return_value = _ok_chat_response("ok")

        created = self.client.post("/api/v1/chat/threads/", {"title": "Work"}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        thread_id = created.data["id"]
        self.assertFalse(created.data["is_main"])

        self.client.post(
            "/api/v1/chat/messages/",
            {"text": "work stuff", "thread_id": thread_id, "client_msg_id": "w1"},
            format="json",
        )
        pmsg = PendingMessage.objects.get(tenant=self.tenant, channel_user_id=thread_id)
        self.assertEqual(pmsg.payload["user_param"], f"thread:{thread_id}")

    @patch("apps.router.chat_views.check_budget", return_value="personal")
    @patch("apps.router.pending_queue.httpx.post")
    def test_budget_gate_blocks_enqueue(self, mock_post, _mock_budget):
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "spendy", "client_msg_id": "z1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["status"], "error")
        self.assertEqual(resp.data["error"], "budget_exhausted")
        # No work enqueued, no gateway call.
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)
        mock_post.assert_not_called()

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.post("/api/v1/chat/messages/", {"text": "hi"}, format="json")
        self.assertIn(resp.status_code, (401, 403))
