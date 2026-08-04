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
from apps.tenants.test_utils import seed_internal_key


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
    return ({"choices": [{"message": {"content": content}}]}, "openrouter/deepseek/deepseek-v4-flash-0731")


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

    @patch("apps.orchestrator.workspace_envelope.render_context_digest", return_value="## Goals\n- ship it")
    def test_response_includes_spoken_field(self, _digest):
        resp = self.client.get("/api/v1/siri/status/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("spoken", resp.data)
        self.assertIsInstance(resp.data["spoken"], str)
        self.assertTrue(resp.data["spoken"])

    @patch("apps.orchestrator.workspace_envelope.render_context_digest", return_value="## Goals\n- ship it")
    def test_spoken_never_carries_markdown_or_tool_names(self, _digest):
        # Even though the digest (snapshot_md) is full of markdown, directives,
        # and tool calls, the spoken field must be clean — this is the exact TTS
        # failure mode we are fixing.
        from apps.journal.models import Goal, Task

        Task.objects.create(tenant=self.tenant, title="pay [PERSON_1] back", status=Task.Status.OPEN)
        Goal.objects.create(tenant=self.tenant, title="run a marathon", status=Goal.Status.ACTIVE)
        resp = self.client.get("/api/v1/siri/status/")
        spoken = resp.data["spoken"]
        for forbidden in ("#", "*", "`", "_", "nbhd_", "[[", "{status", "[PERSON_", "→"):
            self.assertNotIn(forbidden, spoken, f"spoken leaked {forbidden!r}: {spoken!r}")


class SiriSpokenComposerTest(TestCase):
    """Unit coverage for the deterministic, speech-safe status composer."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)

    def _spoken(self) -> str:
        from apps.router.siri_spoken import compose_spoken_status

        return compose_spoken_status(self.tenant)

    def test_empty_state_is_all_clear(self):
        spoken = self._spoken()
        self.assertIn("caught up", spoken.lower())

    def test_counts_open_tasks_and_active_goals(self):
        from apps.journal.models import Goal, Task

        for i in range(13):
            Task.objects.create(tenant=self.tenant, title=f"t{i}", status=Task.Status.OPEN)
        for i in range(2):
            Goal.objects.create(tenant=self.tenant, title=f"g{i}", status=Goal.Status.ACTIVE)
        spoken = self._spoken()
        self.assertIn("13 open tasks", spoken)
        self.assertIn("2 active goals", spoken)

    def test_singular_pluralization(self):
        from apps.journal.models import Task

        Task.objects.create(tenant=self.tenant, title="only one", status=Task.Status.OPEN)
        spoken = self._spoken()
        self.assertIn("1 open task.", spoken)
        self.assertNotIn("1 open tasks", spoken)

    def test_due_this_week_counted(self):
        from datetime import timedelta

        from django.utils import timezone as tz

        from apps.journal.models import Task

        Task.objects.create(
            tenant=self.tenant,
            title="soon",
            status=Task.Status.OPEN,
            due_date=tz.now().date() + timedelta(days=2),
        )
        Task.objects.create(
            tenant=self.tenant,
            title="later",
            status=Task.Status.OPEN,
            due_date=tz.now().date() + timedelta(days=30),
        )
        spoken = self._spoken()
        self.assertIn("1 is due this week", spoken)

    def test_planned_workouts_gated_on_fuel_enabled(self):
        from datetime import timedelta

        from django.utils import timezone as tz

        from apps.fuel.models import Workout, WorkoutCategory, WorkoutStatus

        Workout.objects.create(
            tenant=self.tenant,
            date=tz.now().date() + timedelta(days=1),
            activity="Push day",
            category=WorkoutCategory.STRENGTH,
            status=WorkoutStatus.PLANNED,
        )
        # Fuel disabled → no workout sentence.
        self.tenant.fuel_enabled = False
        self.tenant.save(update_fields=["fuel_enabled"])
        self.assertNotIn("workout", self._spoken().lower())
        # Fuel enabled → surfaced.
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])
        self.assertIn("1 workout planned this week", self._spoken())

    @override_settings(GRAVITY_ENABLED=True)
    def test_payoff_plan_spoken_when_finance_active(self):
        from datetime import date
        from decimal import Decimal

        from apps.finance.models import PayoffPlan

        PayoffPlan.objects.create(
            tenant=self.tenant,
            strategy=PayoffPlan.Strategy.SNOWBALL,
            monthly_budget=Decimal("500.00"),
            total_debt=Decimal("10000.00"),
            total_interest=Decimal("1000.00"),
            payoff_months=24,
            payoff_date=date(2028, 1, 1),
            is_active=True,
        )
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])
        spoken = self._spoken()
        self.assertIn("payoff plan is active", spoken)
        # Never speak dollar amounts.
        self.assertNotIn("$", spoken)
        self.assertNotIn("10000", spoken)

    @override_settings(GRAVITY_ENABLED=False)
    def test_finance_silent_when_gravity_paused(self):
        from datetime import date
        from decimal import Decimal

        from apps.finance.models import PayoffPlan

        PayoffPlan.objects.create(
            tenant=self.tenant,
            strategy=PayoffPlan.Strategy.SNOWBALL,
            monthly_budget=Decimal("500.00"),
            total_debt=Decimal("10000.00"),
            total_interest=Decimal("1000.00"),
            payoff_months=24,
            payoff_date=date(2028, 1, 1),
            is_active=True,
        )
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])
        # finance_active folds in the GRAVITY kill switch — paused → silent.
        self.assertNotIn("payoff", self._spoken().lower())

    def test_north_star_set_when_confirmed_purpose(self):
        from apps.journal.models import Purpose

        Purpose.objects.create(
            tenant=self.tenant,
            statement="build a life where my work funds time with my kids",
            status=Purpose.Status.CONFIRMED,
        )
        spoken = self._spoken()
        self.assertIn("North Star is set", spoken)
        # The statement itself (may carry PII) is never spoken.
        self.assertNotIn("kids", spoken)

    def test_proposed_purpose_not_spoken(self):
        from apps.journal.models import Purpose

        Purpose.objects.create(
            tenant=self.tenant,
            statement="a hypothesis the user has not confirmed",
            status=Purpose.Status.PROPOSED,
        )
        self.assertNotIn("North Star", self._spoken())

    def test_length_capped(self):
        from apps.journal.models import Goal, Task

        for i in range(200):
            Task.objects.create(tenant=self.tenant, title=f"task number {i}", status=Task.Status.OPEN)
        for i in range(200):
            Goal.objects.create(tenant=self.tenant, title=f"goal number {i}", status=Goal.Status.ACTIVE)
        from apps.router.siri_spoken import SPOKEN_MAX_CHARS

        self.assertLessEqual(len(self._spoken()), SPOKEN_MAX_CHARS)

    def test_no_markdown_or_tool_names_in_output(self):
        from apps.journal.models import Goal, Purpose, Task

        Task.objects.create(tenant=self.tenant, title="**bold** task", status=Task.Status.OPEN)
        Goal.objects.create(tenant=self.tenant, title="# heading goal", status=Goal.Status.ACTIVE)
        Purpose.objects.create(tenant=self.tenant, statement="dir", status=Purpose.Status.CONFIRMED)
        spoken = self._spoken()
        for forbidden in ("#", "*", "`", "nbhd_", "[[", "{"):
            self.assertNotIn(forbidden, spoken)


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
        # The ask was routed to the full tenant agent (Tier 3): the turn was
        # persisted for polling and the inline drain POSTed the tenant gateway.
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="s1")
        self.assertEqual(turn.user_text, "summarize my whole month and email it")
        self.assertTrue(any("/v1/chat/completions" in (c.args[0] if c.args else "") for c in mock_post.call_args_list))
        # Delivered → queue row hard-deleted (delete-on-drain, privacy PR-3).
        self.assertFalse(PendingMessage.objects.filter(tenant=self.tenant).exists())

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
        # Exactly one tenant turn enqueued despite two requests: the queue row
        # is hard-deleted after the inline drain (PR-3), so count the gateway
        # POSTs — the replayed second request must not enqueue/POST again.
        completions = [c for c in mock_post.call_args_list if "/v1/chat/completions" in (c.args[0] if c.args else "")]
        self.assertEqual(len(completions), 1)
        self.assertFalse(PendingMessage.objects.filter(tenant=self.tenant).exists())

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
        seed_internal_key(self.tenant)
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

    # ── per-step partial text (pseudo-streaming) ──────────────────────────

    def _post(self, body):
        return self.client.post(
            self._url(),
            body,
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

    def test_partial_text_written_and_exposed_on_poll(self):
        self._pending()
        resp = self._post({"client_msg_id": "p1", "text": "Thinking about", "seq": 1})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["updated"])
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="p1")
        self.assertEqual(turn.partial_text, "Thinking about")
        self.assertEqual(turn.partial_seq, 1)
        # Surfaced on the client-facing poll endpoint while pending.
        poll = APIClient()
        poll.force_authenticate(user=self.user)
        detail = poll.get("/api/v1/chat/messages/p1/")
        self.assertEqual(detail.data["partial_text"], "Thinking about")
        self.assertEqual(detail.data["partial_seq"], 1)

    def test_partial_text_known_values_are_redacted_before_storage(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Theo Smith"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        self._pending()
        resp = self._post({"client_msg_id": "p1", "text": "Thinking about Theo Smith", "seq": 1})
        self.assertEqual(resp.status_code, 200, resp.content)
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="p1")
        self.assertEqual(turn.partial_text, "Thinking about [PERSON_1]")

    def test_text_only_post_without_phase_accepted(self):
        # A partial-carrying post has no phase; it must NOT hit the empty-phase
        # 400, and it must not clobber a live phase with an empty string.
        turn = self._pending()
        turn.phase = "thinking"
        turn.save(update_fields=["phase"])
        resp = self._post({"client_msg_id": "p1", "text": "hello", "seq": 1})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["updated"])
        turn.refresh_from_db()
        self.assertEqual(turn.partial_text, "hello")
        self.assertEqual(turn.phase, "thinking")  # live phase preserved

    def test_stale_and_duplicate_seq_ignored(self):
        self._pending()
        self.assertTrue(self._post({"client_msg_id": "p1", "text": "abcde", "seq": 5}).data["updated"])
        # Stale (lower) seq — ignored, no rewind.
        resp_stale = self._post({"client_msg_id": "p1", "text": "abc", "seq": 3})
        self.assertEqual(resp_stale.status_code, 200)
        self.assertFalse(resp_stale.data["updated"])
        # Duplicate (equal) seq — ignored.
        resp_dup = self._post({"client_msg_id": "p1", "text": "abcXX", "seq": 5})
        self.assertFalse(resp_dup.data["updated"])
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="p1")
        self.assertEqual(turn.partial_text, "abcde")
        self.assertEqual(turn.partial_seq, 5)
        # A strictly-newer seq applies.
        self.assertTrue(self._post({"client_msg_id": "p1", "text": "abcdefgh", "seq": 6}).data["updated"])
        turn.refresh_from_db()
        self.assertEqual(turn.partial_text, "abcdefgh")
        self.assertEqual(turn.partial_seq, 6)

    def test_partial_text_truncated_to_32k(self):
        self._pending()
        self._post({"client_msg_id": "p1", "text": "x" * 40000, "seq": 1})
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="p1")
        self.assertEqual(len(turn.partial_text), 32000)

    def test_invalid_or_missing_seq_ignores_partial(self):
        # text without a valid positive seq carries no applyable partial; with no
        # phase either, the post is the empty 400.
        self._pending()
        self.assertEqual(self._post({"client_msg_id": "p1", "text": "hi", "seq": 0}).status_code, 400)
        self.assertEqual(self._post({"client_msg_id": "p1", "text": "hi", "seq": "nope"}).status_code, 400)
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="p1")
        self.assertEqual(turn.partial_text, "")
        self.assertEqual(turn.partial_seq, 0)

    def test_quick_reply_marker_stripped_from_streaming_partial_text(self):
        """The streaming partial_text bubble must never flash the raw
        [[quick-replies: ...]] marker — whether it's already complete or
        still being typed (unclosed) — while status is still PENDING (F2)."""
        self._pending()
        # Step 1: a COMPLETE trailing marker.
        resp1 = self._post(
            {"client_msg_id": "p1", "text": "Save both changes?\n[[quick-replies: Save both | No thanks]]", "seq": 1}
        )
        self.assertTrue(resp1.data["updated"])
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="p1")
        self.assertEqual(turn.partial_text, "Save both changes?")
        self.assertNotIn("quick-replies", turn.partial_text)

        # Step 2: a still-typing, UNCLOSED marker fragment at the very end.
        resp2 = self._post({"client_msg_id": "p1", "text": "Save both changes?\n[[quick-repl", "seq": 2})
        self.assertTrue(resp2.data["updated"])
        turn.refresh_from_db()
        self.assertEqual(turn.partial_text, "Save both changes?")
        self.assertNotIn("[[", turn.partial_text)

        # Surfaced on the client-facing poll endpoint too — never the raw marker.
        poll = APIClient()
        poll.force_authenticate(user=self.user)
        detail = poll.get("/api/v1/chat/messages/p1/")
        self.assertEqual(detail.data["partial_text"], "Save both changes?")

    def test_partial_not_exposed_after_terminal(self):
        # Serializer gates partial_text/seq on PENDING — a ready row reports '' / 0
        # regardless of any DB residue.
        turn = self._pending()
        turn.partial_text = "leftover stream"
        turn.partial_seq = 9
        turn.status = AppChatMessage.Status.READY
        turn.reply_text = "final"
        turn.save(update_fields=["partial_text", "partial_seq", "status", "reply_text"])
        poll = APIClient()
        poll.force_authenticate(user=self.user)
        detail = poll.get("/api/v1/chat/messages/p1/")
        self.assertEqual(detail.data["partial_text"], "")
        self.assertEqual(detail.data["partial_seq"], 0)

    @override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
    def test_final_reply_clears_partial_text(self):
        from apps.router.pending_queue import _store_ios_turn_reply

        turn = self._pending()
        turn.partial_text = "partial so far"
        turn.partial_seq = 4
        turn.save(update_fields=["partial_text", "partial_seq"])
        pm = PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(self.thread.id),
            payload={"message_text": "hi", "client_msg_id": "p1"},
            delivery_status=PendingMessage.Status.PENDING,
        )
        _store_ios_turn_reply(self.tenant, [pm], "here is the final answer")
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.READY)
        self.assertEqual(turn.reply_text, "here is the final answer")
        self.assertEqual(turn.partial_text, "")

    def test_no_client_msg_id_partial_dropped_on_fallback_but_phase_narrates(self):
        # Cross-channel bleed guard: with NO client_msg_id and NO live IOS lease
        # (the newest-PENDING fallback), the turn actually in flight may be a
        # Telegram/LINE turn — writing its reply text into an unrelated PENDING
        # app row would surface another channel's private reply as this turn's
        # stream. So the low-sensitivity PHASE still narrates on the fallback, but
        # the partial reply TEXT is DROPPED.
        turn = self._pending()
        resp = self._post({"phase": "composing", "text": "secret from another channel", "seq": 1})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["updated"])  # phase applied
        turn.refresh_from_db()
        self.assertEqual(turn.phase, "composing")  # phase narrates on fallback
        self.assertEqual(turn.partial_text, "")  # partial text NOT attributed
        self.assertEqual(turn.partial_seq, 0)

    def test_no_client_msg_id_partial_written_with_in_flight_lease(self):
        # The happy path the plugin actually rides: no client_msg_id, but the
        # in-flight app turn's thread holds a live drain lease → the partial text
        # attributes deterministically to that thread's PENDING row.
        turn = self._pending()
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(self.thread.id),
            payload={"message_text": "hi"},
            delivery_status=PendingMessage.Status.PENDING,
            delivery_in_flight_until=timezone.now() + timedelta(seconds=120),
        )
        resp = self._post({"text": "streaming so far", "seq": 1})  # no client_msg_id
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["updated"])
        turn.refresh_from_db()
        self.assertEqual(turn.partial_text, "streaming so far")
        self.assertEqual(turn.partial_seq, 1)
