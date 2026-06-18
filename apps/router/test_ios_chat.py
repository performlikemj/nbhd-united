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


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSChatContextTest(TestCase):
    """The context-digest endpoint: what makes the PRIVATE on-device mode
    know who the user is. Data flows down to the device; no prompt text
    ever flows out to a model provider."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_returns_compact_digest(self):
        resp = self.client.get("/api/v1/chat/context/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("Current local time", resp.data["context_md"])
        self.assertEqual(resp.data["max_chars"], 6000)
        self.assertIn("generated_at", resp.data)
        # Chat reads must never be HTTP-cached (same rule as message polls).
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_digest_contains_real_conversation_state(self):
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            local_date=tenant_today(self.tenant),
            user_text="I aced the big interview today",
            reply_text="Congratulations!",
        )
        resp = self.client.get("/api/v1/chat/context/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("aced the big interview", resp.data["context_md"])

    def test_rehydrates_placeholders_and_skips_privacy_section(self):
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])
        ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            local_date=tenant_today(self.tenant),
            user_text="met [PERSON_1] for lunch",
        )

        resp = self.client.get("/api/v1/chat/context/")
        self.assertEqual(resp.status_code, 200, resp.content)
        # The device has no entity map: placeholders must arrive rehydrated…
        self.assertIn("met Alice for lunch", resp.data["context_md"])
        self.assertNotIn("[PERSON_1]", resp.data["context_md"])
        # …and the container-only placeholder instructions (which promise a
        # restoration layer that doesn't exist on this path) must be absent.
        self.assertNotIn("## Privacy Placeholders", resp.data["context_md"])

    def test_conversation_digest_survives_budget_pressure(self):
        from apps.orchestrator.envelope_registry import EnvelopeSection

        def section(key, order, body, heading=None):
            return EnvelopeSection(
                key=key,
                heading=heading or f"## {key}",
                render=lambda t: body,
                enabled=lambda t: True,
                refresh_on=(),
                order=order,
            )

        fakes = [
            section("bulky_one", 10, "z" * 600),
            section("bulky_two", 20, "z" * 600),
            section("bulky_three", 30, "z" * 600),
            section("conversation_digest", 65, "today: real talk", heading="## Conversation so far"),
        ]
        with patch("apps.orchestrator.workspace_envelope.all_sections", return_value=fakes):
            resp = self.client.get("/api/v1/chat/context/?max_chars=1000")

        self.assertEqual(resp.status_code, 200, resp.content)
        md = resp.data["context_md"]
        self.assertLessEqual(len(md), 1000)
        # The conversation digest renders LAST but is the most load-bearing
        # context for a client-side model — bulky early sections must not
        # starve it out of the budget.
        self.assertIn("today: real talk", md)
        self.assertNotIn("bulky_three", md)

    def test_max_chars_is_clamped_and_respected(self):
        low = self.client.get("/api/v1/chat/context/?max_chars=50")
        self.assertEqual(low.data["max_chars"], 1000)
        self.assertLessEqual(len(low.data["context_md"]), 1000)

        high = self.client.get("/api/v1/chat/context/?max_chars=999999")
        self.assertEqual(high.data["max_chars"], 16000)

        junk = self.client.get("/api/v1/chat/context/?max_chars=banana")
        self.assertEqual(junk.data["max_chars"], 6000)

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.get("/api/v1/chat/context/")
        self.assertIn(resp.status_code, (401, 403))


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSOnDeviceTurnRecordTest(TestCase):
    """Recording turns that already happened on the client's own model.

    The on-device assistant is a first-class channel: its turns land in
    thread history and the conversation digest, but nothing is enqueued to
    the tenant container and no model budget is consumed."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.router.conversation_capture.schedule_user_md_refresh")
    def test_records_ready_turn_without_enqueue(self, mock_refresh):
        resp = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "log my run", "reply_text": "Logged it.", "client_msg_id": "od1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["status"], "ready")
        self.assertEqual(resp.data["source"], "on_device")
        self.assertEqual(resp.data["reply_text"], "Logged it.")
        self.assertIsNotNone(resp.data["replied_at"])

        # Nothing routed to the tenant container.
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)
        # The conversation digest gets the same debounced USER.md push a
        # captured Telegram/LINE turn triggers.
        mock_refresh.assert_called_once()

    def test_idempotent_on_client_msg_id(self):
        first = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "one", "reply_text": "r", "client_msg_id": "dup-od"},
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "two", "reply_text": "r2", "client_msg_id": "dup-od"},
            format="json",
        )
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(
            AppChatMessage.objects.filter(tenant=self.tenant, client_msg_id="dup-od").count(),
            1,
        )

    def test_turn_lands_in_thread_history_and_digest(self):
        self.client.post(
            "/api/v1/chat/turns/",
            {"text": "planned tomorrow's workout offline", "reply_text": "Nice plan."},
            format="json",
        )
        main = ChatThread.objects.get(tenant=self.tenant, is_main=True)
        self.assertIsNotNone(main.last_active_at)

        history = self.client.get(f"/api/v1/chat/threads/{main.id}/messages/")
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual(len(history.data["messages"]), 1)
        self.assertEqual(history.data["messages"][0]["source"], "on_device")

        from apps.router.conversation_capture import build_conversation_digest

        digest = build_conversation_digest(self.tenant)
        self.assertIn("planned tomorrow's workout", digest)

    @patch("apps.router.chat_views.check_budget", return_value="personal")
    def test_no_budget_gate(self, _mock_budget):
        # An over-budget tenant can still RECORD on-device turns — the reply
        # was produced on the device; no platform model spend is involved.
        resp = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "offline while over budget", "reply_text": "ok"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["status"], "ready")

    def test_validation(self):
        empty = self.client.post("/api/v1/chat/turns/", {"reply_text": "r"}, format="json")
        self.assertEqual(empty.status_code, 400)

        # Over-long content is TRUNCATED, never rejected: the turn already
        # happened; losing the record entirely is worse than losing its tail.
        long_turn = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "y" * 9000, "reply_text": "x" * 20000},
            format="json",
        )
        self.assertEqual(long_turn.status_code, 201, long_turn.content)
        self.assertEqual(len(long_turn.data["user_text"]), 8000)
        self.assertEqual(len(long_turn.data["reply_text"]), 16000)

        bad_id = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "hi", "client_msg_id": "z" * 65},
            format="json",
        )
        self.assertEqual(bad_id.status_code, 400)
        self.assertEqual(bad_id.data["error"], "invalid_client_msg_id")

        bad_body = self.client.post("/api/v1/chat/turns/", ["not", "a", "dict"], format="json")
        self.assertEqual(bad_body.status_code, 400)
        self.assertEqual(bad_body.data["error"], "invalid_body")

    def test_occurred_at_backdates_outbox_delayed_turns(self):
        from datetime import timedelta

        from django.utils import timezone

        yesterday = timezone.now() - timedelta(days=1)
        resp = self.client.post(
            "/api/v1/chat/turns/",
            {
                "text": "chatted on the plane",
                "reply_text": "noted",
                "occurred_at": yesterday.isoformat(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id=resp.data["client_msg_id"])
        self.assertEqual(turn.created_at, yesterday)
        self.assertEqual(turn.replied_at, yesterday)

        # Unparsable / future / ancient timestamps fall back to delivery time.
        for bad in ("not-a-date", (timezone.now() + timedelta(days=2)).isoformat()):
            r = self.client.post(
                "/api/v1/chat/turns/",
                {"text": f"turn {bad}", "occurred_at": bad},
                format="json",
            )
            self.assertEqual(r.status_code, 201, r.content)
            row = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id=r.data["client_msg_id"])
            self.assertGreater(row.created_at, timezone.now() - timedelta(minutes=1))

    def test_concurrent_duplicate_returns_existing(self):
        from django.db import IntegrityError

        first = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "one", "client_msg_id": "race-od"},
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)

        # Simulate losing the existence-check race: the row appears between
        # the .first() check and the INSERT.
        with (
            patch.object(AppChatMessage.objects, "filter") as mock_filter,
            patch.object(AppChatMessage.objects, "create", side_effect=IntegrityError("dup")),
        ):
            mock_filter.return_value.first.return_value = None
            resp = self.client.post(
                "/api/v1/chat/turns/",
                {"text": "one", "client_msg_id": "race-od"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["client_msg_id"], "race-od")

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.post("/api/v1/chat/turns/", {"text": "hi"}, format="json")
        self.assertIn(resp.status_code, (401, 403))


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class ChatSinceFeedTest(TestCase):
    """The flat cross-channel ``GET /api/v1/chat/messages/?since=`` history feed
    (W2): unions app + Telegram/LINE + cron turns into one ascending, dedup-able,
    cursor-paginated stream so the iOS app sees every channel."""

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.router.models import ChatThread

        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.main = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        self._base = timezone.now() - timedelta(hours=2)
        self._td = timedelta

    def _at(self, minutes: int):
        return self._base + self._td(minutes=minutes)

    def _app_turn(self, *, cid, user_text, reply_text, minute, status="ready"):
        m = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.main,
            client_msg_id=cid,
            user_text=user_text,
            reply_text=reply_text,
            status=status,
        )
        AppChatMessage.objects.filter(pk=m.pk).update(created_at=self._at(minute))
        return m

    def _conv_turn(self, *, channel, user_text, reply_text, minute):
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        t = ConversationTurn.objects.create(
            tenant=self.tenant,
            channel=channel,
            channel_user_id="123",
            local_date=tenant_today(self.tenant),
            user_text=user_text,
            reply_text=reply_text,
        )
        ConversationTurn.objects.filter(pk=t.pk).update(created_at=self._at(minute))
        return t

    def _cron_send(self, *, message_text, minute):
        from apps.router.models import ProactiveOutbound

        p = ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text=message_text,
            job_name="Morning Briefing",
        )
        ProactiveOutbound.objects.filter(pk=p.pk).update(created_at=self._at(minute))
        return p

    def _get(self, since=None, limit=None):
        params = {}
        if since is not None:
            params["since"] = since
        if limit is not None:
            params["limit"] = limit
        return self.client.get("/api/v1/chat/messages/", params)

    def test_requires_auth(self):
        resp = APIClient().get("/api/v1/chat/messages/")
        self.assertIn(resp.status_code, (401, 403))

    def test_empty_since_unions_all_channels_ascending(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        self._conv_turn(channel="telegram", user_text="tg hi", reply_text="tg yo", minute=2)
        self._cron_send(message_text="Good morning!", minute=3)

        resp = self._get()
        self.assertEqual(resp.status_code, 200, resp.content)
        msgs = resp.data["messages"]
        # app(user, assistant) + telegram(user, assistant) + cron(assistant) = 5
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant", "assistant"])
        self.assertEqual([m["source"] for m in msgs], ["app", "app", "telegram", "telegram", "cron"])
        self.assertEqual(msgs[0]["text"], "ping")
        self.assertEqual(msgs[1]["text"], "pong")
        self.assertEqual(msgs[4]["text"], "Good morning!")
        # Strictly ascending created_at.
        stamps = [m["created_at"] for m in msgs]
        self.assertEqual(stamps, sorted(stamps))
        # Cursor returned for the next poll.
        self.assertIsNotNone(resp.data["cursor"])

    def test_client_msg_id_on_both_device_rows_not_other_channels(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        self._conv_turn(channel="telegram", user_text="tg hi", reply_text="tg yo", minute=2)

        msgs = self._get().data["messages"]
        app_user, app_asst, tg_user, tg_asst = msgs
        # BOTH halves of a device-originated turn carry the originating client_msg_id
        # so the client can dedup each optimistic row by (client_msg_id, role).
        self.assertEqual(app_user["client_msg_id"], "a1")
        self.assertEqual(app_asst["client_msg_id"], "a1")
        # Other-channel rows were never written locally → no correlation key.
        self.assertNotIn("client_msg_id", tg_user)
        self.assertNotIn("client_msg_id", tg_asst)

    def test_non_app_rows_map_to_main_thread(self):
        self._conv_turn(channel="line", user_text="hi", reply_text="yo", minute=1)
        self._cron_send(message_text="ping", minute=2)
        msgs = self._get().data["messages"]
        for m in msgs:
            self.assertEqual(m["thread_id"], str(self.main.id))

    def test_pending_app_turn_emits_only_user_row(self):
        # A turn still awaiting its reply shows the user's sent message but no
        # (empty) assistant row.
        self._app_turn(cid="p1", user_text="working?", reply_text="", minute=1, status="pending")
        msgs = self._get().data["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["text"], "working?")

    def test_error_app_turn_emits_only_user_row(self):
        self._app_turn(cid="e1", user_text="spendy", reply_text="", minute=1, status="error")
        msgs = self._get().data["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user"])

    def test_ids_are_stable_across_calls(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        first = [m["id"] for m in self._get().data["messages"]]
        second = [m["id"] for m in self._get().data["messages"]]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), len(first))  # globally unique

    def test_same_timestamp_cluster_is_fully_paged(self):
        # Several turns sharing the EXACT created_at (e.g. an offline outbox
        # flushed with one occurred_at) must ALL be paged through — never
        # truncated off the fetch window and silently skipped. Regression for
        # the keyset cluster-loss bug.
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        ts = self._at(5)
        for i in range(3):
            t = ConversationTurn.objects.create(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="123",
                local_date=tenant_today(self.tenant),
                user_text=f"u{i}",
                reply_text=f"r{i}",
            )
            ConversationTurn.objects.filter(pk=t.pk).update(created_at=ts)

        # Single-shot read sees all 6 (3 turns x user+assistant).
        single = sorted(m["id"] for m in self._get(limit=100).data["messages"])
        self.assertEqual(len(single), 6)

        # A paginated walk at the smallest page size must return the SAME id set.
        seen = []
        cursor = None
        for _ in range(50):  # generous bound; converges well before this
            resp = self._get(since=cursor, limit=1)
            page = resp.data["messages"]
            if not page:
                break
            seen.extend(m["id"] for m in page)
            cursor = resp.data["cursor"]
        self.assertEqual(sorted(seen), single)
        self.assertEqual(len(seen), 6)  # no loss, no dupes

    def test_object_cursor_restarts_from_beginning(self):
        # A corrupted cursor that base64+JSON-decodes to an OBJECT (not a list)
        # must restart from the beginning, never 500 (which would wedge the
        # polling client). Regression for the uncaught-KeyError path.
        import base64
        import json

        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        bad = base64.urlsafe_b64encode(json.dumps({"x": 1}).encode()).decode()
        resp = self._get(since=bad)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["messages"]), 2)

    def test_cursor_pagination_walks_without_dupes(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        self._conv_turn(channel="telegram", user_text="tg hi", reply_text="tg yo", minute=2)
        self._cron_send(message_text="Good morning!", minute=3)

        seen = []
        cursor = None
        for _ in range(10):  # generous bound; should converge in 3 pages
            resp = self._get(since=cursor, limit=2)
            self.assertEqual(resp.status_code, 200, resp.content)
            page = resp.data["messages"]
            if not page:
                break
            self.assertLessEqual(len(page), 2)  # server-bounded page size
            seen.extend(m["id"] for m in page)
            cursor = resp.data["cursor"]

        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)  # no duplicates across pages

    def test_empty_page_echoes_cursor_and_does_not_advance(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        first = self._get(limit=100)
        cursor = first.data["cursor"]
        self.assertEqual(len(first.data["messages"]), 2)

        # Re-poll from the tail: nothing new → empty page, SAME cursor echoed.
        again = self._get(since=cursor)
        self.assertEqual(again.data["messages"], [])
        self.assertEqual(again.data["cursor"], cursor)

    def test_limit_is_server_bounded(self):
        for i in range(5):
            self._app_turn(cid=f"a{i}", user_text=f"u{i}", reply_text=f"r{i}", minute=i + 1)
        # Ask for more than the cap → clamped (we only have 10 rows here, but the
        # request must not error and must honor the bound semantics).
        resp = self._get(limit=9999)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertLessEqual(len(resp.data["messages"]), 100)

    def test_malformed_cursor_restarts_from_beginning(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        resp = self._get(since="not-a-valid-cursor")
        self.assertEqual(resp.status_code, 200, resp.content)
        # Lenient: garbage cursor → from the beginning, not a 4xx wedge.
        self.assertEqual(len(resp.data["messages"]), 2)

    def test_on_device_turns_appear_as_app_source(self):
        self._app_turn(cid="od1", user_text="logged offline", reply_text="noted", minute=1, status="ready")
        AppChatMessage.objects.filter(client_msg_id="od1").update(source="on_device")
        msgs = self._get().data["messages"]
        self.assertEqual([m["source"] for m in msgs], ["app", "app"])

    def test_tenant_isolation(self):
        # A different tenant's turns must never leak into this feed.
        other_user = _make_user()
        other_tenant = _make_tenant(other_user)
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        ConversationTurn.objects.create(
            tenant=other_tenant,
            channel="telegram",
            channel_user_id="999",
            local_date=tenant_today(other_tenant),
            user_text="secret",
            reply_text="leak?",
        )
        self._app_turn(cid="a1", user_text="mine", reply_text="ok", minute=1)
        msgs = self._get().data["messages"]
        texts = [m["text"] for m in msgs]
        self.assertNotIn("secret", texts)
        self.assertNotIn("leak?", texts)


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class IOSDrainDropPushTest(TestCase):
    """Drain-level terminal drops must not strand an iOS turn as PENDING.

    When a queued iOS turn is dropped past the delivery-attempts cap or aged out
    as stale, the apology helpers have no channel-native push for iOS — so they
    must flip the AppChatMessage to ERROR and fire the generic 'couldn't finish'
    APNs push, or the client polls a spinner forever with no notification."""

    def setUp(self):
        from apps.router.models import ChatThread

        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")

    def _pending_turn(self, cid):
        from apps.router.models import PendingMessage

        turn = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=cid,
            user_text="do the thing",
            status="pending",
        )
        # Unsaved is fine: the apology helper only reads .channel / .payload.
        pmsg = PendingMessage(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(self.thread.id),
            payload={"client_msg_id": cid},
        )
        return turn, pmsg

    def test_dropped_ios_turn_errors_and_pushes(self):
        from apps.router.pending_queue import _send_apology_for_dropped_pending_message

        turn, pmsg = self._pending_turn("d1")
        with patch("apps.router.push_views.notify_app_reply_error") as mock_notify:
            _send_apology_for_dropped_pending_message(self.tenant, pmsg)
        turn.refresh_from_db()
        self.assertEqual(turn.status, "error")
        self.assertEqual(turn.error, "dropped")
        mock_notify.assert_called_once_with(self.tenant, ["d1"])

    def test_stale_ios_turn_errors_and_pushes(self):
        from apps.router.pending_queue import _send_apology_for_stale_pending_message

        turn, pmsg = self._pending_turn("s1")
        with patch("apps.router.push_views.notify_app_reply_error") as mock_notify:
            _send_apology_for_stale_pending_message(self.tenant, pmsg, 700.0)
        turn.refresh_from_db()
        self.assertEqual(turn.status, "error")
        self.assertEqual(turn.error, "stale")
        mock_notify.assert_called_once_with(self.tenant, ["s1"])
