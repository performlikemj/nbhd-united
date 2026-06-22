"""Tests for the Siri tiered-responder (Tier 0 status, Tier 2 fast responder)
and the agent-activity-stream progress callback.

Adversarial coverage: auth gating, the escalate-vs-answer fork and every
"can't answer fast → escalate" fall-through (sentinel, empty reply, model
error), escalation idempotency + budget gate, and the internal progress
callback's "never mutate a finished turn" invariant.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.router.models import AppChatMessage, PendingMessage
from apps.tenants.models import Tenant, User


def _make_user() -> User:
    return User.objects.create_user(
        username=f"siri_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        preferred_channel="telegram",
    )


def _make_tenant(user: User) -> Tenant:
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-siri.example.com",
    )


def _completion(content: str):
    """A chat_completion() return value: (response_json, model_used)."""
    return ({"choices": [{"message": {"content": content}}]}, "openrouter/deepseek/deepseek-v4-flash")


def _ok_drain_response(text: str = "agent reply"):
    resp = MagicMock()
    resp.status_code = 200
    resp.is_success = True
    resp.json.return_value = {"choices": [{"message": {"content": text}}], "usage": {}, "model": "test"}
    resp.raise_for_status = MagicMock()
    return resp


class SiriStatusTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_requires_auth(self):
        resp = APIClient().get("/api/v1/siri/status/")
        self.assertIn(resp.status_code, (401, 403))

    @patch("apps.orchestrator.workspace_envelope.render_context_digest", return_value="GOALS: ship Siri")
    def test_returns_snapshot(self, _digest):
        resp = self.client.get("/api/v1/siri/status/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["snapshot_md"], "GOALS: ship Siri")
        self.assertIn("generated_at", resp.data)

    @patch("apps.pii.redactor.rehydrate_text", return_value="REAL NAME state")
    @patch("apps.orchestrator.workspace_envelope.render_context_digest", return_value="[PERSON_1] state")
    def test_rehydrates_pii_when_entity_map_present(self, _digest, mock_rehydrate):
        # Give the tenant an entity map so the rehydrate branch fires.
        self.tenant.pii_entity_map = {"PERSON_1": "MJ"}
        self.tenant.save(update_fields=["pii_entity_map"])
        resp = self.client.get("/api/v1/siri/status/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["snapshot_md"], "REAL NAME state")
        mock_rehydrate.assert_called_once()


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class SiriRespondTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_requires_auth(self):
        resp = APIClient().post("/api/v1/siri/respond/", {"intent": "hi"}, format="json")
        self.assertIn(resp.status_code, (401, 403))

    def test_empty_intent_rejected(self):
        resp = self.client.post("/api/v1/siri/respond/", {"intent": "   "}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "empty_intent")

    def test_overlong_intent_rejected(self):
        resp = self.client.post("/api/v1/siri/respond/", {"intent": "x" * 1001}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "intent_too_long")

    @patch("apps.router.siri_views._rehydrated_snapshot", return_value="STATE")
    @patch("apps.common.openrouter.chat_completion", return_value=_completion("You have two tasks due today."))
    def test_fast_answer_returns_without_persisting(self, _cc, _snap):
        resp = self.client.post("/api/v1/siri/respond/", {"intent": "what's due?"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["answered"])
        self.assertFalse(resp.data["escalated"])
        self.assertEqual(resp.data["text"], "You have two tasks due today.")
        # A fast read persists nothing and never enqueues a tenant turn.
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.tenant).count(), 0)
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)

    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.router.siri_views._rehydrated_snapshot", return_value="STATE")
    @patch("apps.common.openrouter.chat_completion", return_value=_completion("[[ESCALATE]]"))
    def test_escalates_on_sentinel(self, _cc, _snap, mock_post):
        mock_post.return_value = _ok_drain_response()
        resp = self.client.post(
            "/api/v1/siri/respond/",
            {"intent": "summarize my whole month and email it", "client_msg_id": "s1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data["answered"])
        self.assertTrue(resp.data["escalated"])
        self.assertEqual(resp.data["client_msg_id"], "s1")
        # The ask was routed to the full tenant agent (Tier 3).
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="s1")
        self.assertEqual(turn.user_text, "summarize my whole month and email it")
        self.assertTrue(PendingMessage.objects.filter(tenant=self.tenant, channel=PendingMessage.Channel.IOS).exists())

    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.router.siri_views._rehydrated_snapshot", return_value="STATE")
    @patch("apps.common.openrouter.chat_completion", side_effect=RuntimeError("openrouter down"))
    def test_escalates_on_model_error(self, _cc, _snap, mock_post):
        mock_post.return_value = _ok_drain_response()
        resp = self.client.post("/api/v1/siri/respond/", {"intent": "anything", "client_msg_id": "s2"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["escalated"])
        self.assertTrue(AppChatMessage.objects.filter(tenant=self.tenant, client_msg_id="s2").exists())

    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.router.siri_views._rehydrated_snapshot", return_value="STATE")
    @patch("apps.common.openrouter.chat_completion", return_value=_completion("   "))
    def test_escalates_on_empty_reply(self, _cc, _snap, mock_post):
        mock_post.return_value = _ok_drain_response()
        resp = self.client.post("/api/v1/siri/respond/", {"intent": "anything", "client_msg_id": "s3"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["escalated"])

    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.router.siri_views._rehydrated_snapshot", return_value="STATE")
    @patch("apps.common.openrouter.chat_completion", return_value=_completion("[[ESCALATE]]"))
    def test_escalation_is_idempotent(self, _cc, _snap, mock_post):
        mock_post.return_value = _ok_drain_response()
        body = {"intent": "deep thing", "client_msg_id": "dup"}
        self.client.post("/api/v1/siri/respond/", body, format="json")
        self.client.post("/api/v1/siri/respond/", body, format="json")
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.tenant, client_msg_id="dup").count(), 1)
        # Exactly one tenant turn enqueued despite two requests.
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 1)

    @patch("apps.router.chat_views.check_budget", return_value="over budget")
    @patch("apps.router.siri_views._rehydrated_snapshot", return_value="STATE")
    @patch("apps.common.openrouter.chat_completion", return_value=_completion("[[ESCALATE]]"))
    def test_escalation_budget_gated(self, _cc, _snap, _budget):
        resp = self.client.post("/api/v1/siri/respond/", {"intent": "deep", "client_msg_id": "b1"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["escalated"])
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="b1")
        self.assertEqual(turn.status, AppChatMessage.Status.ERROR)
        self.assertEqual(turn.error, "budget_exhausted")
        # Over budget → nothing enqueued, no container woken.
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)

    @patch("apps.router.siri_views._rehydrated_snapshot", return_value="STATE")
    @patch("apps.common.openrouter.chat_completion", return_value=_completion("[[ESCALATE]] needs the calendar tool"))
    def test_sentinel_with_trailing_reason_still_escalates(self, _cc, _snap):
        with patch("apps.router.pending_queue.httpx.post", return_value=_ok_drain_response()):
            resp = self.client.post("/api/v1/siri/respond/", {"intent": "x", "client_msg_id": "s4"}, format="json")
        self.assertTrue(resp.data["escalated"])

    @patch("apps.router.siri_views._rehydrated_snapshot", return_value="STATE")
    @patch("apps.common.openrouter.chat_completion", return_value=_completion("Sure, I can [[escalate]] help"))
    def test_midreply_mixedcase_sentinel_stripped_not_leaked(self, _cc, _snap):
        # A stray mixed-case sentinel NOT at the start → still an answer, but the
        # marker must be stripped (case-insensitively), never spoken to the user.
        resp = self.client.post("/api/v1/siri/respond/", {"intent": "help me"}, format="json")
        self.assertTrue(resp.data["answered"])
        self.assertNotIn("[[", resp.data["text"])
        self.assertNotIn("escalate", resp.data["text"].lower())


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class ChatMessageIdempotencyTest(TestCase):
    """Regression: idempotency must precede thread validation in ChatMessageView
    (a retry with a stale/invalid thread_id replays the existing turn, not 404)."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.router.pending_queue.httpx.post")
    def test_retry_with_invalid_thread_replays_existing(self, mock_post):
        mock_post.return_value = _ok_drain_response()
        r1 = self.client.post("/api/v1/chat/messages/", {"text": "hi", "client_msg_id": "k1"}, format="json")
        self.assertEqual(r1.status_code, 201, r1.content)
        # Same client_msg_id, but a valid-format UUID that resolves to no thread.
        r2 = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "hi", "client_msg_id": "k1", "thread_id": "00000000-0000-0000-0000-000000000000"},
            format="json",
        )
        self.assertEqual(r2.status_code, 200, r2.content)
        self.assertEqual(r2.data["client_msg_id"], "k1")


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class ChatProgressEventTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        from apps.router.models import ChatThread

        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        self.client = APIClient()

    def _url(self):
        return f"/api/v1/internal/runtime/{self.tenant.id}/chat/progress/"

    def _pending(self, client_msg_id="p1") -> AppChatMessage:
        return AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=client_msg_id,
            user_text="hi",
            status=AppChatMessage.Status.PENDING,
        )

    def test_missing_internal_key_rejected(self):
        self._pending()
        resp = self.client.post(self._url(), {"client_msg_id": "p1", "phase": "thinking"}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_updates_pending_turn(self):
        self._pending()
        resp = self.client.post(
            self._url(),
            {"client_msg_id": "p1", "phase": "tool", "detail": "searching your journal"},
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["updated"])
        # Surfaced on the client-facing poll endpoint.
        poll = APIClient()
        poll.force_authenticate(user=self.user)
        detail = poll.get("/api/v1/chat/messages/p1/")
        self.assertEqual(detail.data["phase"], "tool")
        self.assertEqual(detail.data["phase_detail"], "searching your journal")

    def test_never_mutates_finished_turn(self):
        turn = self._pending()
        turn.status = AppChatMessage.Status.READY
        turn.reply_text = "done"
        turn.save(update_fields=["status", "reply_text"])
        resp = self.client.post(
            self._url(),
            {"client_msg_id": "p1", "phase": "thinking"},
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["updated"])
        turn.refresh_from_db()
        self.assertEqual(turn.phase, "")

    def test_unknown_turn_is_noop(self):
        resp = self.client.post(
            self._url(),
            {"client_msg_id": "ghost", "phase": "thinking"},
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["updated"])

    def test_missing_fields_rejected(self):
        resp = self.client.post(
            self._url(),
            {"client_msg_id": "p1"},  # no phase
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 400)

    def test_no_client_msg_id_fallback_narrates_newest_when_no_lease(self):
        # FALLBACK path: when no thread holds a live drain lease (e.g. the lease
        # raced/expired), the control plane narrates the most-recent PENDING turn
        # so a real progress event is never dropped.
        older = self._pending("old")
        AppChatMessage.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(minutes=5))
        newer = self._pending("new")
        resp = self.client.post(
            self._url(),
            {"phase": "tool", "detail": "checking your journal"},  # no client_msg_id
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["updated"])
        newer.refresh_from_db()
        older.refresh_from_db()
        self.assertEqual(newer.phase, "tool")
        self.assertEqual(newer.phase_detail, "checking your journal")
        self.assertEqual(older.phase, "")  # no lease → fallback narrates newest

    def test_no_client_msg_id_narrates_in_flight_thread_not_newest(self):
        # router-chat#2: with no client_msg_id, narrate the IN-FLIGHT thread (one
        # holding a live drain lease), NOT merely the newest PENDING turn. Here an
        # OLDER thread A is in flight while a NEWER thread B is only queued — A
        # must win even though B is newer, so B's spinner doesn't show premature
        # progress for a turn that hasn't started.
        from apps.router.models import ChatThread

        # Thread A (older) — actually in flight (leased PendingMessage).
        msg_a = self._pending("a")
        AppChatMessage.objects.filter(pk=msg_a.pk).update(created_at=timezone.now() - timedelta(minutes=5))
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(self.thread.id),
            payload={"message_text": "hi a"},
            delivery_status=PendingMessage.Status.PENDING,
            delivery_in_flight_until=timezone.now() + timedelta(seconds=120),
        )

        # Thread B (newer) — only queued, no live lease.
        thread_b = ChatThread.objects.create(tenant=self.tenant, user=self.user, title="B")
        msg_b = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread_b,
            client_msg_id="b",
            user_text="hi b",
            status=AppChatMessage.Status.PENDING,
        )
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(thread_b.id),
            payload={"message_text": "hi b"},
            delivery_status=PendingMessage.Status.PENDING,
            delivery_in_flight_until=None,  # queued, not yet claimed
        )

        resp = self.client.post(
            self._url(),
            {"phase": "tool", "detail": "searching your journal"},  # no client_msg_id
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["updated"])
        msg_a.refresh_from_db()
        msg_b.refresh_from_db()
        self.assertEqual(msg_a.phase, "tool")  # in-flight thread narrated (despite being older)
        self.assertEqual(msg_b.phase, "")  # newer-but-queued thread NOT narrated
