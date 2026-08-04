"""Tests for the per-tenant message serialization queue (PR #431).

Why this exists
---------------

OpenClaw's claude-cli backend rejects concurrent turns on a single
session. When a user sends message #2 before message #1's claude turn
completes, claude raised "Claude CLI live session is already handling a
turn" — pre-#427 that fell back silently to MiniMax; post-#427 it
errored to the user. Either is broken UX for any real conversation.

The queue serializes per ``(tenant, channel, channel_user_id)`` so the
second message waits for the first to land before being forwarded as a
follow-up turn.

These tests cover the four guarantees in the PR #431 brief:
  - two messages in flight → only one POST at a time
  - first completes → second fires (order preserved)
  - tenant A's queue doesn't block tenant B
  - the in-flight lease + attempts cap behave like ``BufferedMessage``
    (PR #430) so concurrent QStash retries don't fire duplicate POSTs
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import (
    _PROVISION_MAX_WAIT_SECONDS,
    _WAKE_BOOT_GRACE_SECONDS,
    _WAKE_DEFER_SECONDS,
    drain_pending_messages_for_tenant_task,
    enqueue_message_for_tenant,
)
from apps.tenants.models import Tenant, User


def _make_user(line_user_id: str | None = None, telegram_chat_id: int | None = None) -> User:
    return User.objects.create_user(
        username=f"pq_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        line_user_id=line_user_id,
        telegram_chat_id=telegram_chat_id,
        preferred_channel="line" if line_user_id else "telegram",
    )


def _make_tenant(user: User, container_fqdn: str = "oc-pq.example.com") -> Tenant:
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn=container_fqdn,
    )


def _ok_chat_response(text: str = "ok"):
    resp = MagicMock()
    resp.status_code = 200
    resp.is_success = True
    resp.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {},
        "model": "test",
    }
    resp.raise_for_status = MagicMock()
    return resp


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class PendingMessageEnqueueTest(TestCase):
    """``enqueue_message_for_tenant`` should insert a row and (in the
    sync-fallback test path) drive the drain through to delivery."""

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_enqueue_inserts_row_and_drains_on_publish(self, mock_post, _mock_send):
        mock_post.return_value = _ok_chat_response("hello back")

        user = _make_user(line_user_id="U_enq")
        tenant = _make_tenant(user)

        msg = enqueue_message_for_tenant(
            tenant=tenant,
            channel="line",
            channel_user_id="U_enq",
            payload={
                "message_text": "hi",
                "user_param": "U_enq",
                "user_timezone": "UTC",
            },
            user_text_excerpt="hi",
        )

        self.assertIsInstance(msg, PendingMessage)
        # publish_task in tests has no QStash -> sync-fallback drain. The drain
        # ran inline so the row should now be delivered AND hard-deleted
        # (delete-on-drain privacy sweep, PR-3): the transient queue must not
        # retain (redacted) user text past delivery.
        self.assertFalse(
            PendingMessage.objects.filter(id=msg.id).exists(),
            "delivered row must be hard-deleted on drain",
        )
        self.assertEqual(mock_post.call_count, 1)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class PendingMessageInFlightLockTest(TestCase):
    """The per-row in-flight lease must prevent a concurrent drain from
    re-firing the chat completion while the first attempt is still
    mid-POST.

    Same shape as ``DeliverBufferedInFlightLockTest`` (PR #430) — the
    queue reuses the lease pattern."""

    @patch("apps.router.pending_queue._is_tenant_container_live", return_value=True)
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_concurrent_drain_skips_message_with_live_lease(self, mock_post, _mock_send, mock_live):
        """While the first drain is mid-POST, a second concurrent drain
        must observe the live lease and skip the row instead of firing
        a duplicate /v1/chat/completions at the container."""
        user = _make_user(line_user_id="U_lock")
        tenant = _make_tenant(user)
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_lock",
            payload={
                "message_text": "please reply once",
                "user_param": "U_lock",
                "user_timezone": "UTC",
            },
            user_text="please reply once",
        )

        second_call_result: dict = {}

        def _slow_post(*args, **kwargs):
            # Mid-POST, a second drain fires (e.g. QStash retry, or the
            # next webhook arrival). It must observe the in-flight
            # lease and skip the row.
            second_call_result["data"] = drain_pending_messages_for_tenant_task(
                str(tenant.id),
                "line",
                "U_lock",
            )
            return _ok_chat_response("here you go")

        mock_post.side_effect = _slow_post

        result = drain_pending_messages_for_tenant_task(
            str(tenant.id),
            "line",
            "U_lock",
        )

        # First drain delivered the message.
        self.assertEqual(result["delivered"], 1)
        # Second concurrent drain saw the lease and skipped.
        self.assertEqual(second_call_result["data"]["delivered"], 0)
        self.assertEqual(second_call_result["data"]["skipped_in_flight"], 1)
        # Crucially: only ONE chat completion was POSTed.
        self.assertEqual(mock_post.call_count, 1)
        mock_live.assert_called_once_with(tenant)

        # Delivered → hard-deleted on drain (PR-3 privacy sweep).
        self.assertFalse(PendingMessage.objects.filter(id=msg.id).exists())

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_expired_lease_is_reclaimed_on_next_run(self, mock_post, _mock_send):
        """If a previous drain died after taking the lease but before
        clearing it, the next run (after the lease window) must reclaim
        the row. Stuck rows would block forever otherwise."""
        user = _make_user(line_user_id="U_stale")
        tenant = _make_tenant(user)
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_stale",
            payload={
                "message_text": "hi",
                "user_param": "U_stale",
                "user_timezone": "UTC",
            },
            user_text="hi",
            # Stale lease that elapsed 30 minutes ago.
            delivery_in_flight_until=timezone.now() - timedelta(minutes=30),
        )
        mock_post.return_value = _ok_chat_response("ok")

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_stale")

        self.assertEqual(result["delivered"], 1)
        # Delivered → hard-deleted on drain (PR-3 privacy sweep).
        self.assertFalse(PendingMessage.objects.filter(id=msg.id).exists())


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class PendingMessageOrderingTest(TestCase):
    """Multiple messages for the same key drain as a single coalesced turn.

    Pre-coalesce contract was "N inbound rows → N POSTs in arrival order";
    post-coalesce contract is "N inbound rows → 1 POST whose
    ``content`` contains all N raw texts, delineated, in arrival order".
    The intent — FIFO with no overlapping turns — is preserved; only the
    over-the-wire shape changes.
    """

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_two_messages_coalesce_into_one_post(self, mock_post, _mock_send):
        """Two messages enqueued back-to-back during a cold-start window
        fold into ONE chat-completion POST that lists both texts in
        arrival order."""
        mock_post.return_value = _ok_chat_response("ack")

        user = _make_user(line_user_id="U_order")
        tenant = _make_tenant(user)

        # Insert two rows directly so we can control timestamps.
        m1 = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_order",
            payload={"message_text": "first", "user_param": "U_order", "user_timezone": "UTC"},
            user_text="first",
        )
        m2 = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_order",
            payload={"message_text": "second", "user_param": "U_order", "user_timezone": "UTC"},
            user_text="second",
        )

        result1 = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_order")
        self.assertEqual(result1["delivered"], 2)
        self.assertEqual(result1["batch_size"], 2)

        # Both delivered → hard-deleted on drain (PR-3 privacy sweep).
        self.assertFalse(PendingMessage.objects.filter(id__in=[m1.id, m2.id]).exists())

        # One POST with both texts in arrival order in the coalesced content.
        self.assertEqual(mock_post.call_count, 1)
        content = mock_post.call_args_list[0].kwargs["json"]["messages"][0]["content"]
        first_pos = content.find("first")
        second_pos = content.find("second")
        self.assertNotEqual(first_pos, -1, msg=f"'first' missing from coalesced content: {content!r}")
        self.assertNotEqual(second_pos, -1, msg=f"'second' missing from coalesced content: {content!r}")
        self.assertLess(first_pos, second_pos, msg="texts not in arrival order")
        # Coalesced framing marker — agent must know this is a batched bundle,
        # not a single conversational utterance.
        self.assertIn("rapid succession", content)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class PendingMessageTenantIsolationTest(TestCase):
    """Tenant A's queue must not block tenant B's queue.

    The queue is keyed by (tenant, channel, channel_user_id) so a slow
    (or stuck-in-flight) message for tenant A must not delay tenant B's
    drain at all."""

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_in_flight_message_for_tenant_a_does_not_block_tenant_b(self, mock_post, _mock_send):
        mock_post.return_value = _ok_chat_response("hi B")

        user_a = _make_user(line_user_id="U_A")
        user_b = _make_user(line_user_id="U_B")
        tenant_a = _make_tenant(user_a, container_fqdn="oc-A.example.com")
        tenant_b = _make_tenant(user_b, container_fqdn="oc-B.example.com")

        # A's row is "in flight" — lease held by some other worker.
        PendingMessage.objects.create(
            tenant=tenant_a,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_A",
            payload={"message_text": "A's slow turn", "user_param": "U_A", "user_timezone": "UTC"},
            user_text="A's slow turn",
            delivery_in_flight_until=timezone.now() + timedelta(seconds=180),
        )
        # B's row is fresh.
        b_row = PendingMessage.objects.create(
            tenant=tenant_b,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_B",
            payload={"message_text": "B's question", "user_param": "U_B", "user_timezone": "UTC"},
            user_text="B's question",
        )

        # Drain tenant B's key — A's lease must NOT block this.
        result = drain_pending_messages_for_tenant_task(str(tenant_b.id), "line", "U_B")
        self.assertEqual(result["delivered"], 1)

        # Delivered → hard-deleted on drain (PR-3 privacy sweep).
        self.assertFalse(PendingMessage.objects.filter(id=b_row.id).exists())
        # The container POSTed for B was tenant B's container, not A's.
        url = mock_post.call_args[0][0]
        self.assertIn("oc-B.example.com", url)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class PendingMessageAttemptsCapTest(TestCase):
    """Past the per-row attempts cap, the row is dropped (status=failed)
    so a permanently broken request can't wedge the queue forever — same
    semantics as ``BufferedMessage`` (PR #389 head-of-line incident)."""

    @patch("apps.router.pending_queue._send_apology_for_dropped_pending_message")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_message_past_attempts_cap_dropped_with_apology(self, mock_post, _mock_send, mock_apology):
        user = _make_user(line_user_id="U_cap")
        tenant = _make_tenant(user)
        stuck = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_cap",
            payload={
                "message_text": "this one keeps timing out",
                "user_param": "U_cap",
                "user_timezone": "UTC",
            },
            user_text="this one keeps timing out",
            delivery_attempts=3,
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_cap")

        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["delivered"], 0)
        # Container was NOT contacted for the dropped message.
        mock_post.assert_not_called()
        mock_apology.assert_called_once()
        called_msg = mock_apology.call_args[0][1]
        self.assertEqual(called_msg.id, stuck.id)

        stuck.refresh_from_db()
        self.assertEqual(stuck.delivery_status, PendingMessage.Status.FAILED)

    @patch("apps.router.pending_queue._send_apology_for_dropped_pending_message")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_dropped_head_does_not_block_fresh_messages_behind_it(self, mock_post, mock_send, _mock_apology):
        """A maxed-out message at the head of the queue must NOT
        prevent a fresh message behind it from being delivered. Mirrors
        the BufferedMessage head-of-line guarantee from PR #389."""
        mock_post.return_value = _ok_chat_response("here's the answer")

        user = _make_user(line_user_id="U_head")
        tenant = _make_tenant(user)

        # Older stuck message (at cap) → should be dropped.
        PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_head",
            payload={"message_text": "old stuck", "user_param": "U_head", "user_timezone": "UTC"},
            user_text="old stuck",
            delivery_attempts=3,
        )
        fresh = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_head",
            payload={
                "message_text": "please respond",
                "user_param": "U_head",
                "user_timezone": "UTC",
            },
            user_text="please respond",
        )

        # Drain — first call drops the head, re-schedules; sync fallback
        # processes that re-schedule inline and delivers the fresh row.
        drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_head")

        # Fresh row behind the dropped head was delivered → hard-deleted.
        self.assertFalse(PendingMessage.objects.filter(id=fresh.id).exists())
        mock_send.assert_called()

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_persistent_failure_increments_attempts_without_dropping_until_cap(self, mock_post, _mock_send):
        """Each failed POST should increment ``delivery_attempts`` so
        the row eventually hits the cap rather than retrying forever."""
        mock_post.side_effect = httpx.HTTPError("boom")

        user = _make_user(line_user_id="U_fail")
        tenant = _make_tenant(user)
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_fail",
            payload={"message_text": "hi", "user_param": "U_fail", "user_timezone": "UTC"},
            user_text="hi",
        )

        with self.assertRaises(RuntimeError):
            drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_fail")

        msg.refresh_from_db()
        self.assertGreaterEqual(msg.delivery_attempts, 1)
        # Lease cleared on failure so the next drain can re-claim.
        self.assertIsNone(msg.delivery_in_flight_until)
        # Status still pending — only flips to FAILED past the cap.
        self.assertEqual(msg.delivery_status, PendingMessage.Status.PENDING)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    TELEGRAM_BOT_TOKEN="test-bot-token",
)
class PendingMessageTelegramTest(TestCase):
    """Telegram path uses the same queue; reply delivery goes via the
    queue's own ``relay_ai_response_to_telegram`` (which mirrors LINE's
    helper) rather than the long-lived poller."""

    @patch("apps.router.pending_queue.httpx.post")
    def test_telegram_message_delivered_via_queue(self, mock_post):
        # Two POSTs happen: typing pulse + chat completion. Plus one
        # for the response sendMessage. Use a side_effect that returns
        # an OK chat response on the call to /v1/chat/completions and
        # OK MagicMocks for everything else.
        def _route(url, *args, **kwargs):
            if "/v1/chat/completions" in url:
                return _ok_chat_response("Hi back")
            ok = MagicMock()
            ok.is_success = True
            ok.status_code = 200
            return ok

        mock_post.side_effect = _route

        user = _make_user(telegram_chat_id=42424242)
        tenant = _make_tenant(user)
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="42424242",
            payload={
                "message_text": "hi from telegram",
                "user_param": "42424242",
                "user_timezone": "UTC",
            },
            user_text="hi from telegram",
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "telegram", "42424242")

        self.assertEqual(result["delivered"], 1)
        # Delivered → hard-deleted on drain (PR-3 privacy sweep).
        self.assertFalse(PendingMessage.objects.filter(id=msg.id).exists())

        # At least one POST went to /v1/chat/completions and at least
        # one went to the Telegram Bot API for the reply delivery.
        urls = [call.args[0] for call in mock_post.call_args_list]
        self.assertTrue(any("/v1/chat/completions" in u for u in urls))
        self.assertTrue(any("api.telegram.org" in u for u in urls))

    @patch("apps.router.pending_queue.httpx.post")
    def test_telegram_reply_sent_as_rendered_html(self, mock_post):
        """The wire payload must be Telegram HTML — no raw markdown leaks."""
        from apps.router.pending_queue import _send_telegram_markdown

        ok = MagicMock()
        ok.is_success = True
        ok.status_code = 200
        mock_post.return_value = ok

        _send_telegram_markdown(
            99,
            "## Study Kit\n\n---\n\n**Bold** and a [link](https://x.com)\n\n| A | B |\n|---|---|\n| 1 | 2 |",
        )

        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertTrue(send_calls)
        payload = send_calls[0].kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        body = payload["text"]
        # Rendered, not raw.
        self.assertIn("<b>Study Kit</b>", body)
        self.assertIn('<a href="https://x.com">link</a>', body)
        self.assertIn("<pre>", body)  # table → monospace grid
        for raw in ("##", "---", "**", "|---|"):
            self.assertNotIn(raw, body)

    @patch("apps.router.pending_queue.httpx.post")
    def test_quick_reply_marker_stripped_never_leaks(self, mock_post):
        """Telegram has no button transport for the generic quick-replies
        marker (iOS-only) — it must be stripped, never sent raw."""
        from apps.router.pending_queue import relay_ai_response_to_telegram

        ok = MagicMock()
        ok.is_success = True
        ok.status_code = 200
        mock_post.return_value = ok

        user = _make_user(telegram_chat_id=555)
        tenant = _make_tenant(user)

        relay_ai_response_to_telegram(
            tenant, 555, "Save both changes?\n[[quick-replies: Save both | Change something | No thanks]]"
        )

        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertTrue(send_calls)
        bodies = " ".join(c.kwargs["json"]["text"] for c in send_calls)
        self.assertNotIn("quick-replies", bodies)
        self.assertIn("Save both changes?", bodies)

    @patch("apps.router.pending_queue.httpx.post")
    def test_journal_link_marker_stripped_never_leaks(self, mock_post):
        """The journal deep-link chip is iOS-only — Telegram has no transport
        for it, so the marker must be stripped, never sent raw."""
        from apps.router.pending_queue import relay_ai_response_to_telegram

        ok = MagicMock()
        ok.is_success = True
        ok.status_code = 200
        mock_post.return_value = ok

        user = _make_user(telegram_chat_id=556)
        tenant = _make_tenant(user)

        relay_ai_response_to_telegram(
            tenant,
            556,
            ("Logged today's note.\n[[journal-link: daily|2026-07-13|Morning Report]]\nGood luck tomorrow."),
        )

        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertTrue(send_calls)
        bodies = " ".join(c.kwargs["json"]["text"] for c in send_calls)
        self.assertNotIn("journal-link", bodies)
        self.assertIn("Logged today's note.", bodies)
        self.assertIn("Good luck tomorrow.", bodies)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    TELEGRAM_BOT_TOKEN="test-bot-token",
)
class PendingMessageStaleHibernationReconcileTest(TestCase):
    """A successful (non-credit-limit) gateway response proves the
    container is awake, so the drain must clear any lingering
    ``hibernated_at``.

    Regression: the Telegram *poller* path has no wake step (poller →
    enqueue → drain, no ``handle_hibernated_message``), so an out-of-band
    revision activate could leave ``hibernated_at`` set indefinitely while
    the tenant chatted normally. ``update_container`` then short-circuited
    on its ``if tenant.hibernated_at: return False`` guard and every
    Telegram self-update reported "the update failed" (canary 148ccf1c,
    2026-06-03). The drain now reconciles the flag on proven liveness.
    """

    def _telegram_chat_route(self, chat_text: str = "Hi back"):
        def _route(url, *args, **kwargs):
            if "/v1/chat/completions" in url:
                return _ok_chat_response(chat_text)
            ok = MagicMock()
            ok.is_success = True
            ok.status_code = 200
            return ok

        return _route

    @patch("apps.router.pending_queue.httpx.post")
    def test_live_response_clears_stale_hibernated_at(self, mock_post):
        mock_post.side_effect = self._telegram_chat_route()

        user = _make_user(telegram_chat_id=51515151)
        tenant = _make_tenant(user)
        # Stale flag: container is actually serving but the DB still says
        # hibernated (the bug condition).
        Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())

        PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="51515151",
            payload={
                "message_text": "hello",
                "user_param": "51515151",
                "user_timezone": "UTC",
            },
            user_text="hello",
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "telegram", "51515151")

        self.assertEqual(result["delivered"], 1)
        tenant.refresh_from_db()
        self.assertIsNone(
            tenant.hibernated_at,
            "a live gateway response must clear the stale hibernation flag",
        )

    @patch("apps.router.pending_queue._handle_openrouter_credit_limit")
    @patch("apps.router.pending_queue.httpx.post")
    def test_credit_limit_does_not_clear_hibernated_at(self, mock_post, _mock_credit):
        # 402 on the chat completion → OpenRouter credit-limit path, which
        # *intentionally* hibernates for budget. The reconcile must NOT undo
        # that (gateway_responded is False for this early-return).
        def _route(url, *args, **kwargs):
            if "/v1/chat/completions" in url:
                resp = MagicMock()
                resp.status_code = 402
                resp.is_success = False
                resp.text = "insufficient credit"
                return resp
            ok = MagicMock()
            ok.is_success = True
            ok.status_code = 200
            return ok

        mock_post.side_effect = _route

        user = _make_user(telegram_chat_id=52525252)
        tenant = _make_tenant(user)
        hib_at = timezone.now()
        Tenant.objects.filter(id=tenant.id).update(hibernated_at=hib_at)

        PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="52525252",
            payload={
                "message_text": "spend money",
                "user_param": "52525252",
                "user_timezone": "UTC",
            },
            user_text="spend money",
        )

        drain_pending_messages_for_tenant_task(str(tenant.id), "telegram", "52525252")

        # The credit-limit handler was invoked, and the reconcile left the
        # hibernation flag in place (did not undo the budget hibernation).
        _mock_credit.assert_called_once()
        tenant.refresh_from_db()
        self.assertIsNotNone(
            tenant.hibernated_at,
            "credit-limit early-return must not be treated as a liveness signal",
        )

    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant")
    @patch("apps.billing.services.check_budget", return_value="")
    @patch("apps.router.pending_queue._looks_like_openrouter_credit_limit", return_value=False)
    @patch("apps.router.pending_queue.httpx.post")
    def test_hibernated_container_404_wakes_and_defers(
        self, mock_post, _mock_credit_check, _mock_budget, mock_wake, mock_publish
    ):
        """The poller path has no wake step, so a message landing on the
        drain while the container is genuinely hibernated (deactivated
        revision → 404) must WAKE the container and defer the drain — not
        burn the attempt cap and drop the message (canary 148ccf1c,
        2026-06-05).
        """

        def _route(url, *args, **kwargs):
            if "/v1/chat/completions" in url:
                resp = MagicMock()
                resp.status_code = 404
                resp.is_success = False
                resp.text = "Not Found"
                resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "404 Not Found", request=MagicMock(), response=resp
                )
                return resp
            ok = MagicMock()
            ok.is_success = True
            ok.status_code = 200
            return ok

        mock_post.side_effect = _route

        user = _make_user(telegram_chat_id=53535353)
        tenant = _make_tenant(user)
        Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())

        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="53535353",
            payload={
                "message_text": "wake up",
                "user_param": "53535353",
                "user_timezone": "UTC",
            },
            user_text="wake up",
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "telegram", "53535353")

        # Woke the container instead of dropping the message.
        self.assertTrue(result.get("woke"))
        mock_wake.assert_called_once()

        # Message preserved: still PENDING, attempt counter NOT burned,
        # lease released so the deferred re-drain can re-claim it.
        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.PENDING)
        self.assertEqual(msg.delivery_attempts, 0)
        self.assertIsNone(msg.delivery_in_flight_until)

        # Drain rescheduled after the boot delay (not immediately).
        drain_calls = [
            c for c in mock_publish.call_args_list if c.args and c.args[0] == "drain_pending_messages_for_tenant"
        ]
        self.assertTrue(drain_calls, "expected a deferred drain reschedule")
        self.assertEqual(drain_calls[-1].kwargs.get("delay_seconds"), _WAKE_DEFER_SECONDS)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=False)
    @patch("apps.billing.services.check_budget", return_value="")
    @patch("apps.router.pending_queue._looks_like_openrouter_credit_limit", return_value=False)
    @patch("apps.router.pending_queue.httpx.post")
    def test_hibernated_container_404_failed_wake_falls_through(
        self, mock_post, _mock_credit_check, _mock_budget, mock_wake, _mock_publish
    ):
        """If the wake genuinely fails (returns False), the drain must NOT defer
        for free — that re-arms the 404→wake→fail loop forever. It falls through
        to the bounded failure path, which burns an attempt (and ultimately
        drops + apologizes) so the message can't wedge (canary 148ccf1c,
        2026-06-25).
        """

        def _route(url, *args, **kwargs):
            if "/v1/chat/completions" in url:
                resp = MagicMock()
                resp.status_code = 404
                resp.is_success = False
                resp.text = "Not Found"
                resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "404 Not Found", request=MagicMock(), response=resp
                )
                return resp
            ok = MagicMock()
            ok.is_success = True
            ok.status_code = 200
            return ok

        mock_post.side_effect = _route

        user = _make_user(telegram_chat_id=53535354)
        tenant = _make_tenant(user)
        Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())

        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="53535354",
            payload={"message_text": "wake up", "user_param": "53535354", "user_timezone": "UTC"},
            user_text="wake up",
        )

        # Failed wake → falls through to the failure path, which raises to
        # surface a non-2xx for the QStash retry.
        with self.assertRaises(RuntimeError):
            drain_pending_messages_for_tenant_task(str(tenant.id), "telegram", "53535354")

        mock_wake.assert_called_once()
        # The attempt counter advanced (bounded), lease released, still pending.
        msg.refresh_from_db()
        self.assertEqual(msg.delivery_attempts, 1)
        self.assertEqual(msg.delivery_status, PendingMessage.Status.PENDING)
        self.assertIsNone(msg.delivery_in_flight_until)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class PendingMessageDrainNoOpTest(TestCase):
    """Calling drain when the queue is empty for the key must be a
    no-op (no POSTs, no errors). Important because QStash may fire
    duplicate drain triggers."""

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_drain_with_empty_queue_is_noop(self, mock_post, _mock_send):
        user = _make_user(line_user_id="U_empty")
        tenant = _make_tenant(user)

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_empty")

        self.assertEqual(result["delivered"], 0)
        self.assertEqual(result["skipped_in_flight"], 0)
        mock_post.assert_not_called()


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class PendingMessageTimeoutResolutionTest(TestCase):
    """``_resolve_chat_timeout`` must apply the longer
    ``REASONING_MODEL_TIMEOUT`` for BYO Claude and reasoning models —
    same intent as PR #430's ``_resolve_chat_timeout`` for buffered
    delivery."""

    def test_default_minimax_uses_default_timeout(self):
        from apps.billing.constants import DEFAULT_CHAT_TIMEOUT, MINIMAX_MODEL
        from apps.router.pending_queue import _resolve_chat_timeout

        user = _make_user(line_user_id="U_to_minimax")
        tenant = _make_tenant(user)
        tenant.preferred_model = MINIMAX_MODEL
        tenant.save(update_fields=["preferred_model"])

        self.assertEqual(_resolve_chat_timeout(tenant), DEFAULT_CHAT_TIMEOUT)

    def test_byo_anthropic_sonnet_uses_reasoning_timeout(self):
        """If BYO_SLOW_MODELS isn't yet defined (PR #430 not landed),
        the resolver still falls back gracefully — the explicit fallback
        in pending_queue keeps imports safe."""
        try:
            from apps.billing.constants import BYO_SLOW_MODELS  # noqa: F401
        except ImportError:
            self.skipTest("BYO_SLOW_MODELS not yet defined (PR #430 hasn't merged)")

        from apps.billing.constants import (
            ANTHROPIC_SONNET_MODEL,
            REASONING_MODEL_TIMEOUT,
        )
        from apps.router.pending_queue import _resolve_chat_timeout

        user = _make_user(line_user_id="U_to_sonnet")
        tenant = _make_tenant(user)
        tenant.preferred_model = ANTHROPIC_SONNET_MODEL
        tenant.save(update_fields=["preferred_model"])

        self.assertEqual(_resolve_chat_timeout(tenant), REASONING_MODEL_TIMEOUT)


# ---------------------------------------------------------------------------
# Reaper tests — closes the gap when a drain task's original publish
# never made it to QStash (or QStash dropped it into the DLQ pit).
# Canonical bug: 2026-05-23 canary screenshot incident where two 7+h
# stale rows produced "this was already done" replies after gateway
# recovery. Reaper exists to bound how long a stuck row can sit.
# ---------------------------------------------------------------------------


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class ReapStuckInboundMessagesTest(TestCase):
    """``reap_stuck_inbound_messages_task`` republishes drain tasks for
    rows whose original drain never ran."""

    def _make_pending(
        self,
        tenant: Tenant,
        channel_user_id: str,
        age_seconds: int,
        *,
        channel: str = "line",
        in_flight_until=None,
        status: str = PendingMessage.Status.PENDING,
    ) -> PendingMessage:
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=channel,
            channel_user_id=channel_user_id,
            payload={
                "message_text": "test",
                "user_param": channel_user_id,
                "user_timezone": "UTC",
            },
            user_text="test",
            delivery_status=status,
            delivery_in_flight_until=in_flight_until,
        )
        # Bypass auto_now_add to backdate created_at deterministically.
        PendingMessage.objects.filter(id=msg.id).update(
            created_at=timezone.now() - timedelta(seconds=age_seconds),
        )
        msg.refresh_from_db()
        return msg

    @patch("apps.cron.publish.publish_task")
    def test_reaper_ignores_fresh_rows(self, mock_publish):
        from apps.router.pending_queue import reap_stuck_inbound_messages_task

        user = _make_user(line_user_id="U_fresh")
        tenant = _make_tenant(user)
        # 30s old — under the 90s stuck threshold
        self._make_pending(tenant, "U_fresh", age_seconds=30)

        result = reap_stuck_inbound_messages_task()

        self.assertEqual(result["stuck_keys"], 0)
        self.assertEqual(result["republished"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    def test_reaper_republishes_stuck_row(self, mock_publish):
        from apps.router.pending_queue import reap_stuck_inbound_messages_task

        user = _make_user(line_user_id="U_stuck")
        tenant = _make_tenant(user)
        # 5 minutes old, no in-flight lease — the canonical "stuck" case
        self._make_pending(tenant, "U_stuck", age_seconds=300)

        result = reap_stuck_inbound_messages_task()

        self.assertEqual(result["stuck_keys"], 1)
        self.assertEqual(result["republished"], 1)
        self.assertEqual(result["errors"], 0)
        mock_publish.assert_called_once()
        # Verify it republished the drain task with the right key
        args, kwargs = mock_publish.call_args
        self.assertEqual(args[0], "drain_pending_messages_for_tenant")
        self.assertEqual(args[1], str(tenant.id))
        self.assertEqual(args[2], "line")
        self.assertEqual(args[3], "U_stuck")
        self.assertEqual(kwargs.get("retries"), 3)

    @patch("apps.cron.publish.publish_task")
    def test_reaper_skips_rows_with_live_lease(self, mock_publish):
        from apps.router.pending_queue import reap_stuck_inbound_messages_task

        user = _make_user(line_user_id="U_inflight")
        tenant = _make_tenant(user)
        # Row is old, but a concurrent drain is mid-POST (lease alive)
        self._make_pending(
            tenant,
            "U_inflight",
            age_seconds=300,
            in_flight_until=timezone.now() + timedelta(seconds=60),
        )

        result = reap_stuck_inbound_messages_task()

        self.assertEqual(result["stuck_keys"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    def test_reaper_includes_rows_with_expired_lease(self, mock_publish):
        """A row whose lease expired (claim succeeded but POST never
        completed — e.g. worker died mid-flight) still needs reaping."""
        from apps.router.pending_queue import reap_stuck_inbound_messages_task

        user = _make_user(line_user_id="U_expired_lease")
        tenant = _make_tenant(user)
        self._make_pending(
            tenant,
            "U_expired_lease",
            age_seconds=300,
            in_flight_until=timezone.now() - timedelta(seconds=10),
        )

        result = reap_stuck_inbound_messages_task()

        self.assertEqual(result["stuck_keys"], 1)
        self.assertEqual(result["republished"], 1)
        mock_publish.assert_called_once()

    @patch("apps.cron.publish.publish_task")
    def test_reaper_dedups_multiple_stuck_rows_per_key(self, mock_publish):
        """Two stuck rows for the same (tenant, channel, user) get
        ONE drain republish (the drain itself walks the queue FIFO)."""
        from apps.router.pending_queue import reap_stuck_inbound_messages_task

        user = _make_user(line_user_id="U_double")
        tenant = _make_tenant(user)
        self._make_pending(tenant, "U_double", age_seconds=300)
        self._make_pending(tenant, "U_double", age_seconds=180)

        result = reap_stuck_inbound_messages_task()

        self.assertEqual(result["stuck_keys"], 1)
        self.assertEqual(result["republished"], 1)
        mock_publish.assert_called_once()

    @patch("apps.cron.publish.publish_task")
    def test_reaper_ignores_delivered_and_failed_rows(self, mock_publish):
        """Terminal-state rows must never be republished. The reaper
        filters by delivery_status=PENDING."""
        from apps.router.pending_queue import reap_stuck_inbound_messages_task

        user = _make_user(line_user_id="U_done")
        tenant = _make_tenant(user)
        self._make_pending(tenant, "U_done", age_seconds=600, status=PendingMessage.Status.DELIVERED)
        self._make_pending(tenant, "U_done", age_seconds=600, status=PendingMessage.Status.FAILED)

        result = reap_stuck_inbound_messages_task()

        self.assertEqual(result["stuck_keys"], 0)
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    def test_reaper_publishes_distinct_keys_separately(self, mock_publish):
        """Two different (tenant, channel, user) keys → two separate
        republishes so each queue gets its own drain."""
        from apps.router.pending_queue import reap_stuck_inbound_messages_task

        user_a = _make_user(line_user_id="U_a")
        user_b = _make_user(line_user_id="U_b")
        tenant_a = _make_tenant(user_a, container_fqdn="oc-a.example.com")
        tenant_b = _make_tenant(user_b, container_fqdn="oc-b.example.com")
        self._make_pending(tenant_a, "U_a", age_seconds=300)
        self._make_pending(tenant_b, "U_b", age_seconds=300)

        result = reap_stuck_inbound_messages_task()

        self.assertEqual(result["stuck_keys"], 2)
        self.assertEqual(result["republished"], 2)
        self.assertEqual(mock_publish.call_count, 2)

    @patch("apps.cron.publish.publish_task")
    def test_reaper_swallows_individual_publish_errors(self, mock_publish):
        """A per-key publish failure must NOT abort the whole sweep —
        the next minute's tick will retry that key, and other keys
        must still get a chance this tick."""
        from apps.router.pending_queue import reap_stuck_inbound_messages_task

        # First call raises; second succeeds
        mock_publish.side_effect = [Exception("qstash down"), None]

        user_a = _make_user(line_user_id="U_err_a")
        user_b = _make_user(line_user_id="U_err_b")
        tenant_a = _make_tenant(user_a, container_fqdn="oc-erra.example.com")
        tenant_b = _make_tenant(user_b, container_fqdn="oc-errb.example.com")
        self._make_pending(tenant_a, "U_err_a", age_seconds=300)
        self._make_pending(tenant_b, "U_err_b", age_seconds=300)

        result = reap_stuck_inbound_messages_task()

        self.assertEqual(result["stuck_keys"], 2)
        self.assertEqual(result["republished"], 1)
        self.assertEqual(result["errors"], 1)


# ---------------------------------------------------------------------------
# Stale-message guard tests — when a row is finally claimed but the
# user has long since moved on, don't POST it to OC. Mark it failed and
# send an apology instead. This is the defense-in-depth that prevents
# the canary "responding to questions from hours ago" bug even if the
# reaper itself misfires for some reason.
# ---------------------------------------------------------------------------


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class StaleMessageGuardTest(TestCase):
    """When a drain claims a row older than the staleness threshold,
    no chat completion should fire."""

    @patch("apps.router.pending_queue._send_apology_for_stale_pending_message")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_stale_line_message_skips_oc_and_sends_apology(self, mock_post, _mock_send, mock_apology):
        user = _make_user(line_user_id="U_stale")
        tenant = _make_tenant(user)

        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel="line",
            channel_user_id="U_stale",
            payload={
                "message_text": "old message",
                "user_param": "U_stale",
                "user_timezone": "UTC",
            },
            user_text="old message",
        )
        # 15 minutes old — past the 600s staleness threshold
        PendingMessage.objects.filter(id=msg.id).update(
            created_at=timezone.now() - timedelta(minutes=15),
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_stale")

        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result.get("stale"), 1)
        self.assertEqual(result["delivered"], 0)

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.FAILED)
        self.assertIsNotNone(msg.delivered_at)

        # Critical: no POST to OC for the stale row
        oc_posts = [c for c in mock_post.call_args_list if "/v1/chat/completions" in (c.args[0] if c.args else "")]
        self.assertEqual(oc_posts, [], "stale row must not be POSTed to OC")

        # Apology helper was called with the row + an age_seconds value
        mock_apology.assert_called_once()
        called_args = mock_apology.call_args.args
        self.assertEqual(called_args[0], tenant)
        self.assertEqual(called_args[1].id, msg.id)
        self.assertGreaterEqual(called_args[2], 600)

    @patch("apps.router.pending_queue._send_apology_for_stale_pending_message")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_fresh_message_still_posts_to_oc(self, mock_post, _mock_send, mock_apology):
        """Regression: a row well under the threshold must still POST
        to OC and deliver normally. Sanity check that the stale guard
        didn't accidentally block the happy path."""
        mock_post.return_value = _ok_chat_response("hello back")

        user = _make_user(line_user_id="U_fresh_drain")
        tenant = _make_tenant(user)

        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel="line",
            channel_user_id="U_fresh_drain",
            payload={
                "message_text": "fresh",
                "user_param": "U_fresh_drain",
                "user_timezone": "UTC",
            },
            user_text="fresh",
        )
        # Default created_at is auto_now_add (i.e. ~now) → fresh.

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_fresh_drain")

        self.assertEqual(result["delivered"], 1)
        self.assertIsNone(result.get("stale"))
        # Delivered → hard-deleted on drain (PR-3 privacy sweep).
        self.assertFalse(PendingMessage.objects.filter(id=msg.id).exists())
        mock_apology.assert_not_called()

    @patch("apps.router.pending_queue._send_apology_for_stale_pending_message")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_stale_drain_reschedules_when_more_pending(self, mock_post, _mock_send, mock_apology):
        """A stale row at head of queue must not block fresher rows
        behind it — drain reschedules itself after dropping the stale
        head so the next row gets a chance."""
        from apps.router.pending_queue import _reschedule_drain  # noqa: F401

        mock_post.return_value = _ok_chat_response("hi")

        user = _make_user(line_user_id="U_chain")
        tenant = _make_tenant(user)

        stale = PendingMessage.objects.create(
            tenant=tenant,
            channel="line",
            channel_user_id="U_chain",
            payload={"message_text": "old", "user_param": "U_chain", "user_timezone": "UTC"},
            user_text="old",
        )
        PendingMessage.objects.filter(id=stale.id).update(
            created_at=timezone.now() - timedelta(minutes=20),
        )

        fresh = PendingMessage.objects.create(
            tenant=tenant,
            channel="line",
            channel_user_id="U_chain",
            payload={"message_text": "now", "user_param": "U_chain", "user_timezone": "UTC"},
            user_text="now",
        )

        with patch("apps.router.pending_queue._reschedule_drain") as mock_resched:
            result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_chain")
            self.assertEqual(result.get("stale"), 1)
            # _has_more_pending should have returned True (fresh row exists)
            mock_resched.assert_called_once()

        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stale.delivery_status, PendingMessage.Status.FAILED)
        # Fresh row should still be PENDING (the reschedule would drain it
        # on the next task tick; we don't actually run that here).
        self.assertEqual(fresh.delivery_status, PendingMessage.Status.PENDING)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class PendingMessageColdStartCoalesceTest(TestCase):
    """Cold-start coalescing: N rapid-fire messages during the warm-tenant
    in-flight-lease window fold into one OC turn with delineated content.

    Mirrors the user-perceived UX: agent reads the burst as one combined
    request instead of replying N times to near-identical or
    superseded-by-followup messages.
    """

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_three_messages_coalesce_into_one_post(self, mock_post, _mock_send):
        """Three rapid-fire LINE messages → one POST whose content lists
        all three in arrival order with index + timestamp framing."""
        mock_post.return_value = _ok_chat_response("ack")

        user = _make_user(line_user_id="U_three")
        tenant = _make_tenant(user)
        texts = ["check fuel", "actually also yesterday", "you there?"]
        for txt in texts:
            PendingMessage.objects.create(
                tenant=tenant,
                channel=PendingMessage.Channel.LINE,
                channel_user_id="U_three",
                payload={
                    "message_text": txt,
                    "user_param": "U_three",
                    "user_timezone": "UTC",
                },
                user_text=txt,
            )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_three")
        self.assertEqual(result["delivered"], 3)
        self.assertEqual(result["batch_size"], 3)
        self.assertEqual(mock_post.call_count, 1)

        content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
        # Each raw text present, in arrival order, with index markers.
        last_pos = -1
        for i, txt in enumerate(texts, start=1):
            self.assertIn(txt, content)
            self.assertIn(f"[{i}]", content)
            pos = content.find(txt)
            self.assertGreater(pos, last_pos, msg=f"text #{i} ({txt!r}) out of order")
            last_pos = pos

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_voice_row_breaks_batch(self, mock_post, _mock_send):
        """A voice row in the queue is never coalesced with text. The
        batch ends at the first voice row; voice rows are claimed as
        singletons on a subsequent drain tick."""
        mock_post.return_value = _ok_chat_response("ack")

        user = _make_user(line_user_id="U_voice")
        tenant = _make_tenant(user)
        text1 = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_voice",
            payload={"message_text": "text one", "user_param": "U_voice", "user_timezone": "UTC"},
            user_text="text one",
        )
        text2 = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_voice",
            payload={"message_text": "text two", "user_param": "U_voice", "user_timezone": "UTC"},
            user_text="text two",
        )
        voice = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_voice",
            payload={
                "message_text": "[voice transcript]",
                "user_param": "U_voice",
                "user_timezone": "UTC",
                "is_voice": True,
            },
            user_text="[voice transcript]",
        )

        # First drain: coalesces text1+text2, leaves voice for next tick.
        # Sync-fallback reschedule will drain the voice singleton, then
        # exit (no more pending). Total = 2 POSTs.
        drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_voice")

        # All three delivered → hard-deleted on drain (PR-3 privacy sweep).
        self.assertFalse(PendingMessage.objects.filter(id__in=[text1.id, text2.id, voice.id]).exists())

        # First POST: coalesced text1+text2. Second POST: voice singleton.
        self.assertEqual(mock_post.call_count, 2)
        first_content = mock_post.call_args_list[0].kwargs["json"]["messages"][0]["content"]
        self.assertIn("text one", first_content)
        self.assertIn("text two", first_content)
        self.assertNotIn("voice transcript", first_content)
        second_content = mock_post.call_args_list[1].kwargs["json"]["messages"][0]["content"]
        self.assertIn("voice transcript", second_content)
        # The voice row drained as a singleton (no coalescing framing).
        self.assertNotIn("rapid succession", second_content)

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_voice_head_is_singleton(self, mock_post, _mock_send):
        """A voice row at the head of the queue stays a singleton even
        when text rows are queued behind it. Voice and text don't fold
        together regardless of arrival order."""
        mock_post.return_value = _ok_chat_response("ack")

        user = _make_user(line_user_id="U_vhead")
        tenant = _make_tenant(user)
        voice = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_vhead",
            payload={
                "message_text": "[voice transcript]",
                "user_param": "U_vhead",
                "user_timezone": "UTC",
                "is_voice": True,
            },
            user_text="[voice transcript]",
        )
        text_row = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_vhead",
            payload={"message_text": "follow up text", "user_param": "U_vhead", "user_timezone": "UTC"},
            user_text="follow up text",
        )

        drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_vhead")

        # Both delivered → hard-deleted on drain (PR-3 privacy sweep).
        self.assertFalse(PendingMessage.objects.filter(id__in=[voice.id, text_row.id]).exists())

        # Two POSTs total — voice alone, then text alone (no coalescing
        # framing since the next batch was a singleton too).
        self.assertEqual(mock_post.call_count, 2)
        first_content = mock_post.call_args_list[0].kwargs["json"]["messages"][0]["content"]
        self.assertIn("voice transcript", first_content)
        self.assertNotIn("follow up text", first_content)
        # Singleton drains preserve the pre-coalesce on-the-wire shape:
        # no coalescing marker.
        self.assertNotIn("rapid succession", first_content)

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_message_counter_bumps_by_batch_size(self, mock_post, _mock_send):
        """``messages_today`` / ``messages_this_month`` count user-perceived
        sends, not LLM turns. A coalesced batch of N rows must bump the
        per-tenant counters by N (otherwise the user sends 5 messages,
        sees 1 reply, and "5 messages today" undercounts to 1)."""
        # Response with real token counts so _record_usage_safe actually
        # makes it past the early-exit.
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "ack"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            "model": "test",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        user = _make_user(line_user_id="U_counter")
        tenant = _make_tenant(user)
        for txt in ("one", "two", "three"):
            PendingMessage.objects.create(
                tenant=tenant,
                channel=PendingMessage.Channel.LINE,
                channel_user_id="U_counter",
                payload={"message_text": txt, "user_param": "U_counter", "user_timezone": "UTC"},
                user_text=txt,
            )

        tenant.refresh_from_db()
        before_today = tenant.messages_today
        before_month = tenant.messages_this_month

        drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_counter")

        tenant.refresh_from_db()
        self.assertEqual(tenant.messages_today - before_today, 3)
        self.assertEqual(tenant.messages_this_month - before_month, 3)
        # Only one POST despite three messages — that's the coalesce win.
        self.assertEqual(mock_post.call_count, 1)

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.get")
    @patch("apps.router.pending_queue.httpx.post")
    def test_in_flight_lease_blocks_batch_claim(self, mock_post, mock_get, _mock_send):
        """If ANY row in the key's queue has a live in-flight lease, the
        batch claim must return empty (skipped_in_flight) instead of
        racing the concurrent drain. Preserves the single-turn invariant
        the Claude CLI backend requires."""
        live_response = MagicMock()
        live_response.status_code = 200
        mock_get.return_value = live_response

        user = _make_user(line_user_id="U_lease")
        tenant = _make_tenant(user)
        lease_expiry = timezone.now() + timedelta(minutes=5)
        leased = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_lease",
            payload={"message_text": "leased", "user_param": "U_lease", "user_timezone": "UTC"},
            user_text="leased",
            delivery_in_flight_until=lease_expiry,
        )
        fresh = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_lease",
            payload={"message_text": "fresh", "user_param": "U_lease", "user_timezone": "UTC"},
            user_text="fresh",
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_lease")
        # Nothing delivered — both rows held back.
        self.assertEqual(result["delivered"], 0)
        self.assertEqual(result["skipped_in_flight"], 2)
        mock_post.assert_not_called()
        mock_get.assert_called_once_with(
            f"https://{tenant.container_fqdn}/health",
            timeout=3.0,
        )

        leased.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(leased.delivery_status, PendingMessage.Status.PENDING)
        self.assertEqual(fresh.delivery_status, PendingMessage.Status.PENDING)
        self.assertEqual(leased.delivery_in_flight_until, lease_expiry)
        # Fresh row didn't get a lease — the batch claim is all-or-nothing.
        self.assertIsNone(fresh.delivery_in_flight_until)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    @patch("apps.billing.services.check_budget", return_value="")
    @patch("apps.router.pending_queue._looks_like_openrouter_credit_limit", return_value=False)
    @patch("apps.router.pending_queue.httpx.get")
    @patch("apps.router.pending_queue.httpx.post")
    def test_in_flight_lease_is_broken_when_container_is_down(
        self,
        mock_post,
        mock_get,
        _mock_credit,
        _mock_budget,
        mock_wake,
        _mock_send,
        mock_publish,
    ):
        health_response = MagicMock()
        health_response.status_code = 404
        mock_get.return_value = health_response

        chat_response = MagicMock()
        chat_response.status_code = 404
        chat_response.text = "Not Found"
        chat_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404 Not Found",
            request=MagicMock(),
            response=chat_response,
        )
        mock_post.return_value = chat_response

        user = _make_user(line_user_id="U_down_lease")
        tenant = _make_tenant(user)
        Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())
        leased = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_down_lease",
            payload={
                "message_text": "leased",
                "user_param": "U_down_lease",
                "user_timezone": "UTC",
            },
            user_text="leased",
            delivery_in_flight_until=timezone.now() + timedelta(minutes=5),
        )
        fresh = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_down_lease",
            payload={
                "message_text": "fresh",
                "user_param": "U_down_lease",
                "user_timezone": "UTC",
            },
            user_text="fresh",
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_down_lease")

        self.assertTrue(result.get("woke"))
        mock_get.assert_called_once_with(
            f"https://{tenant.container_fqdn}/health",
            timeout=3.0,
        )
        mock_post.assert_called_once()
        mock_wake.assert_called_once()

        leased.refresh_from_db()
        fresh.refresh_from_db()
        for row in (leased, fresh):
            self.assertEqual(row.delivery_status, PendingMessage.Status.PENDING)
            self.assertEqual(row.delivery_attempts, 0)
            self.assertIsNone(row.delivery_in_flight_until)

        drain_calls = [
            call
            for call in mock_publish.call_args_list
            if call.args and call.args[0] == "drain_pending_messages_for_tenant"
        ]
        self.assertEqual(drain_calls[-1].kwargs.get("delay_seconds"), _WAKE_DEFER_SECONDS)


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class WakeBootGraceTest(TestCase):
    """Container-down errors shortly after a hibernation wake mean "still
    booting", not "delivery failed": the drain must release the lease,
    keep the attempt counters (cap is only 3; OpenClaw cold boots can take
    30-150s), and retry shortly. Past the grace window a down container is
    a real failure again. iOS turns get ``waking_at`` stamped so polling
    clients can show honest wake copy instead of indefinite typing dots."""

    @staticmethod
    def _container_404(url, *args, **kwargs):
        if "/v1/chat/completions" in url:
            resp = MagicMock()
            resp.status_code = 404
            resp.is_success = False
            resp.text = "Not Found"
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "404 Not Found", request=MagicMock(), response=resp
            )
            return resp
        ok = MagicMock()
        ok.is_success = True
        ok.status_code = 200
        return ok

    @patch("apps.cron.publish.publish_task")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    @patch("apps.billing.services.check_budget", return_value="")
    @patch("apps.router.pending_queue.httpx.get")
    @patch("apps.router.pending_queue.httpx.post")
    def test_read_timeout_with_down_container_wakes_and_releases_lease(
        self,
        mock_post,
        mock_get,
        _mock_budget,
        mock_wake,
        _mock_send,
        mock_publish,
    ):
        request = httpx.Request(
            "POST",
            "https://oc-pq.example.com/v1/chat/completions",
        )
        mock_post.side_effect = httpx.ReadTimeout("read timed out", request=request)
        health_response = MagicMock()
        health_response.status_code = 404
        mock_get.return_value = health_response

        user = _make_user(line_user_id="U_read_timeout")
        tenant = _make_tenant(user)
        Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_read_timeout",
            payload={
                "message_text": "please wake",
                "user_param": "U_read_timeout",
                "user_timezone": "UTC",
            },
            user_text="please wake",
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_read_timeout")

        self.assertTrue(result.get("woke"))
        mock_get.assert_called_once_with(
            f"https://{tenant.container_fqdn}/health",
            timeout=3.0,
        )
        mock_wake.assert_called_once()

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.PENDING)
        self.assertEqual(msg.delivery_attempts, 0)
        self.assertIsNone(msg.delivery_in_flight_until)

        drain_calls = [
            call
            for call in mock_publish.call_args_list
            if call.args and call.args[0] == "drain_pending_messages_for_tenant"
        ]
        self.assertEqual(drain_calls[-1].kwargs.get("delay_seconds"), _WAKE_DEFER_SECONDS)

    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.get")
    @patch("apps.router.pending_queue.httpx.post")
    def test_read_timeout_with_live_container_keeps_bounded_failure_semantics(
        self,
        mock_post,
        mock_get,
        _mock_send,
        mock_wake,
    ):
        request = httpx.Request(
            "POST",
            "https://oc-pq.example.com/v1/chat/completions",
        )
        mock_post.side_effect = httpx.ReadTimeout("turn timed out", request=request)
        health_response = MagicMock()
        health_response.status_code = 200
        mock_get.return_value = health_response

        user = _make_user(line_user_id="U_live_timeout")
        tenant = _make_tenant(user)
        Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_live_timeout",
            payload={
                "message_text": "long turn",
                "user_param": "U_live_timeout",
                "user_timezone": "UTC",
            },
            user_text="long turn",
        )

        with self.assertRaises(RuntimeError):
            drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_live_timeout")

        mock_get.assert_called_once_with(
            f"https://{tenant.container_fqdn}/health",
            timeout=3.0,
        )
        mock_wake.assert_not_called()
        msg.refresh_from_db()
        self.assertEqual(msg.delivery_attempts, 1)
        self.assertIsNone(msg.delivery_in_flight_until)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant")
    @patch("apps.router.pending_queue._looks_like_openrouter_credit_limit", return_value=False)
    @patch("apps.router.pending_queue.httpx.post")
    def test_booting_after_wake_defers_without_attempt_burn(self, mock_post, _mock_credit, mock_wake, mock_publish):
        mock_post.side_effect = self._container_404

        user = _make_user(telegram_chat_id=61616161)
        tenant = _make_tenant(user)
        Tenant.objects.filter(id=tenant.id).update(
            hibernated_at=None,
            last_wake_at=timezone.now() - timedelta(seconds=30),
        )

        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="61616161",
            payload={
                "message_text": "hello again",
                "user_param": "61616161",
                "user_timezone": "UTC",
            },
            user_text="hello again",
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "telegram", "61616161")

        self.assertTrue(result.get("booting"))
        # Not hibernated, so no re-wake (the wake already happened).
        mock_wake.assert_not_called()

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.PENDING)
        self.assertEqual(msg.delivery_attempts, 0)
        self.assertIsNone(msg.delivery_in_flight_until)

        drain_calls = [
            c for c in mock_publish.call_args_list if c.args and c.args[0] == "drain_pending_messages_for_tenant"
        ]
        self.assertTrue(drain_calls, "expected a deferred drain reschedule")
        self.assertEqual(drain_calls[-1].kwargs.get("delay_seconds"), _WAKE_DEFER_SECONDS)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.router.pending_queue._looks_like_openrouter_credit_limit", return_value=False)
    @patch("apps.router.pending_queue.httpx.post")
    def test_boot_grace_expired_is_a_real_failure(self, mock_post, _mock_credit, _mock_publish):
        mock_post.side_effect = self._container_404

        user = _make_user(telegram_chat_id=62626262)
        tenant = _make_tenant(user)
        Tenant.objects.filter(id=tenant.id).update(
            hibernated_at=None,
            last_wake_at=timezone.now() - timedelta(seconds=_WAKE_BOOT_GRACE_SECONDS + 60),
        )

        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="62626262",
            payload={
                "message_text": "anyone there",
                "user_param": "62626262",
                "user_timezone": "UTC",
            },
            user_text="anyone there",
        )

        # Past the grace window the normal failure path applies: attempts
        # advance and the drain re-raises so QStash retries with backoff.
        with self.assertRaises(RuntimeError):
            drain_pending_messages_for_tenant_task(str(tenant.id), "telegram", "62626262")

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_attempts, 1)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant")
    @patch("apps.billing.services.check_budget", return_value="")
    @patch("apps.router.pending_queue._looks_like_openrouter_credit_limit", return_value=False)
    @patch("apps.router.pending_queue.httpx.post")
    def test_ios_wake_stamps_waking_at_for_polling_clients(
        self, mock_post, _mock_credit, _mock_budget, _mock_wake, _mock_publish
    ):
        from apps.router.models import AppChatMessage, ChatThread

        mock_post.side_effect = self._container_404

        user = _make_user(telegram_chat_id=63636363)
        tenant = _make_tenant(user)
        Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())

        thread = ChatThread.objects.create(tenant=tenant, user=user, title="", is_main=True)
        turn = AppChatMessage.objects.create(
            tenant=tenant,
            user=user,
            thread=thread,
            client_msg_id="cmid-wake-1",
            user_text="good morning",
        )
        PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(thread.id),
            payload={
                "message_text": "good morning",
                "user_param": f"thread:{thread.id}",
                "user_timezone": "UTC",
                "client_msg_id": "cmid-wake-1",
            },
            user_text="good morning",
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        self.assertTrue(result.get("woke"))
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.PENDING)
        self.assertIsNotNone(turn.waking_at)


@override_settings(NBHD_INTERNAL_API_KEY="test-key", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class DrainDuringProvisioningTest(TestCase):
    """A brand-new tenant's container is built asynchronously (~1 min) and has
    no FQDN yet. Messages that arrive in that window must be BUFFERED + re-driven
    until the container lands — not failed (which silently strands a new user's
    very first message: it goes FAILED, the reaper only re-drives PENDING rows,
    and nothing re-delivers it once the container is up). A tenant that is
    genuinely gone (deprovisioned) still fails fast, as before.

    NOTE: ``_reschedule_drain`` is patched because in tests ``publish_task`` runs
    synchronously (no QStash), so a real re-drive would recurse into the same
    provisioning guard forever. In prod the re-drive is an async QStash task
    (~20s later), so there is no recursion.
    """

    def _provisioning_tenant(self, user):
        return Tenant.objects.create(user=user, status=Tenant.Status.PROVISIONING, container_fqdn="")

    def _line_row(self, tenant, channel_user_id, *, created_at=None):
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id=channel_user_id,
            payload={"message_text": "Hello", "user_param": channel_user_id, "user_timezone": "UTC"},
            user_text="Hello",
        )
        if created_at is not None:
            PendingMessage.objects.filter(pk=msg.pk).update(created_at=created_at)
            msg.refresh_from_db()
        return msg

    @patch("apps.router.pending_queue._reschedule_drain")
    @patch("apps.router.pending_queue.httpx.post")
    def test_provisioning_buffers_and_reschedules_instead_of_failing(self, mock_post, mock_resched):
        user = _make_user(line_user_id="Uprov")
        tenant = self._provisioning_tenant(user)
        msg = self._line_row(tenant, "Uprov")

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "Uprov")

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.PENDING)  # buffered, NOT failed
        self.assertTrue(result.get("provisioning"))
        mock_resched.assert_called_once()  # a re-drive was scheduled
        mock_post.assert_not_called()  # never POSTed to a not-yet-built container

    @patch("apps.router.pending_queue._reschedule_drain")
    @patch("apps.router.pending_queue._send_apology_for_dropped_pending_message")
    @patch("apps.router.pending_queue.httpx.post")
    def test_deprovisioned_tenant_still_fails_fast(self, mock_post, mock_apology, mock_resched):
        user = _make_user(line_user_id="Udep")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.DEPROVISIONING, container_fqdn="")
        msg = self._line_row(tenant, "Udep")

        drain_pending_messages_for_tenant_task(str(tenant.id), "line", "Udep")

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.FAILED)
        mock_resched.assert_not_called()
        mock_post.assert_not_called()
        mock_apology.assert_called_once()

    @patch("apps.router.pending_queue._reschedule_drain")
    @patch("apps.router.pending_queue._send_apology_for_dropped_pending_message")
    @patch("apps.router.pending_queue.httpx.post")
    def test_provisioning_past_max_wait_fails(self, mock_post, mock_apology, mock_resched):
        user = _make_user(line_user_id="Ucap")
        tenant = self._provisioning_tenant(user)
        old = timezone.now() - timedelta(seconds=_PROVISION_MAX_WAIT_SECONDS + 60)
        msg = self._line_row(tenant, "Ucap", created_at=old)

        drain_pending_messages_for_tenant_task(str(tenant.id), "line", "Ucap")

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.FAILED)
        mock_resched.assert_not_called()
        mock_apology.assert_called_once()

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.router.pending_queue._reschedule_drain")
    def test_buffered_message_delivers_once_container_is_up(self, mock_resched, mock_post, _mock_send):
        mock_post.return_value = _ok_chat_response("welcome!")
        user = _make_user(line_user_id="Udeliver")
        tenant = self._provisioning_tenant(user)
        msg = self._line_row(tenant, "Udeliver")

        # During provisioning: buffered, not delivered, no container POST.
        drain_pending_messages_for_tenant_task(str(tenant.id), "line", "Udeliver")
        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.PENDING)
        mock_post.assert_not_called()

        # Container finishes provisioning.
        Tenant.objects.filter(id=tenant.id).update(status=Tenant.Status.ACTIVE, container_fqdn="oc-prov.example.com")

        # The next drain delivers the buffered message — the first "Hello" lands.
        drain_pending_messages_for_tenant_task(str(tenant.id), "line", "Udeliver")

        # Delivered on the second drain → hard-deleted (PR-3 privacy sweep).
        self.assertFalse(PendingMessage.objects.filter(id=msg.id).exists())
        self.assertEqual(mock_post.call_count, 1)

    @patch("apps.router.pending_queue._reschedule_drain")
    @patch("apps.router.pending_queue.httpx.post")
    def test_provisioning_stamps_waking_at_for_ios_polling_clients(self, mock_post, _mock_resched):
        user = _make_user()
        tenant = self._provisioning_tenant(user)
        thread = ChatThread.objects.create(tenant=tenant, user=user, title="", is_main=True)
        turn = AppChatMessage.objects.create(
            tenant=tenant,
            user=user,
            thread=thread,
            client_msg_id="cmid-prov-1",
            user_text="Hello",
        )
        PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(thread.id),
            payload={
                "message_text": "Hello",
                "user_param": f"thread:{thread.id}",
                "user_timezone": "UTC",
                "client_msg_id": "cmid-prov-1",
            },
            user_text="Hello",
        )

        result = drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        self.assertTrue(result.get("provisioning"))
        turn.refresh_from_db()
        # iOS renders "setting up / waking" off waking_at instead of a blind spinner.
        self.assertEqual(turn.status, AppChatMessage.Status.PENDING)
        self.assertIsNotNone(turn.waking_at)
        mock_post.assert_not_called()


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
    NBHD_DISABLE_BACKGROUND_THREADS=True,
)
class PendingTurnTerminalizationTest(TestCase):
    """Queue-first terminal exits atomically terminalize correlated app turns."""

    def _tenant(self, *, status=Tenant.Status.ACTIVE, fqdn="oc-term.example.com"):
        user = _make_user()
        tenant = Tenant.objects.create(user=user, status=status, container_fqdn=fqdn)
        thread = ChatThread.objects.create(tenant=tenant, user=user, is_main=True, title="Main")
        return tenant, thread

    def _pair(
        self,
        tenant,
        thread,
        cid,
        *,
        partial="unfinished",
        app_status=AppChatMessage.Status.PENDING,
        attempts=0,
        age_seconds=None,
        channel=PendingMessage.Channel.IOS,
        channel_user_id=None,
    ):
        turn = AppChatMessage.objects.create(
            tenant=tenant,
            user=tenant.user,
            thread=thread,
            client_msg_id=cid,
            user_text=f"question {cid}",
            status=app_status,
            partial_text=partial,
        )
        queue_row = PendingMessage.objects.create(
            tenant=tenant,
            channel=channel,
            channel_user_id=channel_user_id or str(thread.id),
            payload={
                "message_text": f"question {cid}",
                "user_param": f"thread:{thread.id}",
                "user_timezone": "UTC",
                "client_msg_id": cid,
            },
            user_text=f"question {cid}",
            delivery_attempts=attempts,
        )
        if age_seconds is not None:
            PendingMessage.objects.filter(id=queue_row.id).update(
                created_at=timezone.now() - timedelta(seconds=age_seconds)
            )
            queue_row.refresh_from_db()
        return turn, queue_row

    def _assert_terminal(self, turn, queue_row, error):
        turn.refresh_from_db()
        queue_row.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.ERROR)
        self.assertEqual(turn.error, error)
        self.assertIsNotNone(turn.replied_at)
        self.assertEqual(turn.partial_text, "")
        self.assertEqual(queue_row.delivery_status, PendingMessage.Status.FAILED)
        self.assertIsNotNone(queue_row.delivered_at)
        self.assertIsNone(queue_row.delivery_in_flight_until)

    @patch("apps.router.push_views.notify_app_reply_error")
    def test_no_fqdn_terminalizes_both_models_and_pushes_once(self, mock_notify):
        tenant, thread = self._tenant(status=Tenant.Status.PROVISIONING, fqdn="")
        turn, queue_row = self._pair(
            tenant,
            thread,
            "no-fqdn",
            age_seconds=_PROVISION_MAX_WAIT_SECONDS + 60,
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        self.assertEqual(result["dropped"], 1)
        self._assert_terminal(turn, queue_row, "dropped")
        mock_notify.assert_called_once_with(tenant, ["no-fqdn"])

    def test_suspended_deprovisioning_and_deleted_tenants_terminalize(self):
        for status in (
            Tenant.Status.SUSPENDED,
            Tenant.Status.DEPROVISIONING,
            Tenant.Status.DELETED,
        ):
            with self.subTest(status=status):
                tenant, thread = self._tenant(status=status, fqdn="")
                turn, queue_row = self._pair(tenant, thread, f"gone-{status}")
                drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))
                self._assert_terminal(turn, queue_row, "dropped")

    def test_missing_tenant_is_harmless_noop(self):
        result = drain_pending_messages_for_tenant_task(str(uuid.uuid4()), "ios", "missing-thread")
        self.assertEqual(result, {"delivered": 0, "failed": 0, "dropped": 0, "skipped_in_flight": 0})

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_fqdn_appears_during_terminal_recheck_and_normal_drain_proceeds(self, mock_post, _mock_send):
        from apps.router import pending_queue

        mock_post.return_value = _ok_chat_response("delivered after provision")
        tenant, thread = self._tenant(fqdn="")
        _turn, queue_row = self._pair(
            tenant,
            thread,
            "fqdn-race",
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U-race",
        )
        original = pending_queue._terminalize_failed_queue_rows

        def finish_provisioning(**kwargs):
            Tenant.objects.filter(id=tenant.id).update(container_fqdn="oc-race.example.com")
            return original(**kwargs)

        with patch("apps.router.pending_queue._terminalize_failed_queue_rows", side_effect=finish_provisioning):
            result = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U-race")

        self.assertEqual(result["delivered"], 1)
        self.assertFalse(PendingMessage.objects.filter(id=queue_row.id).exists())
        mock_post.assert_called_once()

    @patch("apps.router.push_views.notify_app_reply_error")
    def test_attempt_cap_terminalizes_both_models(self, mock_notify):
        tenant, thread = self._tenant()
        turn, queue_row = self._pair(tenant, thread, "at-cap", attempts=3)

        with self.captureOnCommitCallbacks(execute=True):
            result = drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        self.assertEqual(result["dropped"], 1)
        self._assert_terminal(turn, queue_row, "dropped")
        mock_notify.assert_called_once_with(tenant, ["at-cap"])

    @patch("apps.router.push_views.notify_app_reply_error")
    def test_stale_path_terminalizes_both_models(self, mock_notify):
        tenant, thread = self._tenant()
        turn, queue_row = self._pair(tenant, thread, "stale", age_seconds=15 * 60)

        with self.captureOnCommitCallbacks(execute=True):
            result = drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        self.assertEqual(result["stale"], 1)
        self._assert_terminal(turn, queue_row, "stale")
        mock_notify.assert_called_once_with(tenant, ["stale"])

    def test_app_update_exception_rolls_queue_row_back_to_pending(self):
        tenant, thread = self._tenant()
        turn, queue_row = self._pair(tenant, thread, "rollback", attempts=3)

        with (
            patch.object(AppChatMessage.objects, "filter") as app_filter,
            self.assertRaisesRegex(RuntimeError, "forced app update failure"),
        ):
            app_filter.return_value.update.side_effect = RuntimeError("forced app update failure")
            drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        queue_row.refresh_from_db()
        turn.refresh_from_db()
        self.assertEqual(queue_row.delivery_status, PendingMessage.Status.PENDING)
        self.assertEqual(turn.status, AppChatMessage.Status.PENDING)

    @patch("apps.router.push_views.notify_app_reply_error")
    def test_repeat_terminalization_is_idempotent_without_double_push(self, mock_notify):
        tenant, thread = self._tenant()
        turn, queue_row = self._pair(tenant, thread, "repeat", attempts=3)

        with self.captureOnCommitCallbacks(execute=True):
            drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))
            drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        self._assert_terminal(turn, queue_row, "dropped")
        mock_notify.assert_called_once_with(tenant, ["repeat"])

    @patch("apps.router.push_views.notify_app_reply_error")
    def test_ready_turn_is_never_overwritten_or_pushed(self, mock_notify):
        tenant, thread = self._tenant()
        turn, queue_row = self._pair(
            tenant,
            thread,
            "already-ready",
            partial="",
            app_status=AppChatMessage.Status.READY,
            attempts=3,
        )
        AppChatMessage.objects.filter(id=turn.id).update(reply_text="finished", replied_at=timezone.now())

        with self.captureOnCommitCallbacks(execute=True):
            drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        turn.refresh_from_db()
        queue_row.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.READY)
        self.assertEqual(turn.reply_text, "finished")
        self.assertEqual(turn.error, "")
        self.assertEqual(queue_row.delivery_status, PendingMessage.Status.FAILED)
        mock_notify.assert_not_called()

    @patch("apps.router.push_views.notify_app_reply_error")
    def test_coalesced_key_terminalizes_every_correlated_turn(self, mock_notify):
        tenant, thread = self._tenant(fqdn="")
        pairs = [self._pair(tenant, thread, cid) for cid in ("batch-1", "batch-2", "batch-3")]

        with self.captureOnCommitCallbacks(execute=True):
            drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        for turn, queue_row in pairs:
            self._assert_terminal(turn, queue_row, "dropped")
        mock_notify.assert_called_once_with(tenant, ["batch-1", "batch-2", "batch-3"])

    def test_other_channel_key_and_tenant_rows_are_untouched(self):
        tenant, thread = self._tenant(fqdn="")
        other_tenant, other_thread = self._tenant(fqdn="")
        target = self._pair(tenant, thread, "target")
        other_key = self._pair(tenant, thread, "other-key", channel_user_id="different-thread")
        other_channel = self._pair(
            tenant,
            thread,
            "other-channel",
            channel=PendingMessage.Channel.LINE,
            channel_user_id=str(thread.id),
        )
        other_owner = self._pair(other_tenant, other_thread, "other-tenant")

        drain_pending_messages_for_tenant_task(str(tenant.id), "ios", str(thread.id))

        self._assert_terminal(*target, "dropped")
        for turn, queue_row in (other_key, other_channel, other_owner):
            turn.refresh_from_db()
            queue_row.refresh_from_db()
            self.assertEqual(turn.status, AppChatMessage.Status.PENDING)
            self.assertEqual(queue_row.delivery_status, PendingMessage.Status.PENDING)


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class StaleAppChatMessageReaperTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")

    def _turn(self, cid, *, age_minutes=21, status=AppChatMessage.Status.PENDING, source=AppChatMessage.Source.TENANT):
        turn = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=cid,
            user_text="question",
            status=status,
            source=source,
            partial_text="residual partial",
        )
        AppChatMessage.objects.filter(id=turn.id).update(created_at=timezone.now() - timedelta(minutes=age_minutes))
        turn.refresh_from_db()
        return turn

    @patch("apps.router.push_views.notify_app_reply_error")
    def test_stale_orphan_reaped_and_repeat_is_idempotent(self, mock_notify):
        from apps.router.pending_queue import reap_stale_app_chat_messages_task

        turn = self._turn("orphan")
        with self.captureOnCommitCallbacks(execute=True):
            first = reap_stale_app_chat_messages_task()
            second = reap_stale_app_chat_messages_task()

        self.assertEqual(first["reaped"], 1)
        self.assertEqual(second["reaped"], 0)
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.ERROR)
        self.assertEqual(turn.error, "stale")
        self.assertIsNotNone(turn.replied_at)
        self.assertEqual(turn.partial_text, "")
        mock_notify.assert_called_once_with(self.tenant, ["orphan"])

    def test_fresh_pending_is_untouched(self):
        from apps.router.pending_queue import reap_stale_app_chat_messages_task

        turn = self._turn("fresh", age_minutes=19)
        self.assertEqual(reap_stale_app_chat_messages_task()["reaped"], 0)
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.PENDING)

    def test_pending_queue_row_excludes_turn_even_with_expired_lease(self):
        from apps.router.pending_queue import reap_stale_app_chat_messages_task

        turn = self._turn("owned-by-queue")
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(self.thread.id),
            payload={"client_msg_id": "owned-by-queue"},
            delivery_in_flight_until=timezone.now() - timedelta(hours=1),
        )

        self.assertEqual(reap_stale_app_chat_messages_task()["reaped"], 0)
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.PENDING)

    def test_ready_row_is_preserved(self):
        from apps.router.pending_queue import reap_stale_app_chat_messages_task

        turn = self._turn("ready", status=AppChatMessage.Status.READY)
        AppChatMessage.objects.filter(id=turn.id).update(reply_text="done", partial_text="")

        self.assertEqual(reap_stale_app_chat_messages_task()["reaped"], 0)
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.READY)
        self.assertEqual(turn.reply_text, "done")

    def test_batch_is_capped_at_200(self):
        from apps.router.pending_queue import reap_stale_app_chat_messages_task

        turns = [self._turn(f"cap-{i}") for i in range(201)]
        self.assertEqual(reap_stale_app_chat_messages_task()["reaped"], 200)
        self.assertEqual(
            AppChatMessage.objects.filter(
                id__in=[turn.id for turn in turns], status=AppChatMessage.Status.PENDING
            ).count(),
            1,
        )


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class CleanupStalePendingMessagesTest(TestCase):
    """The 14-day retention sweeper deletes terminal rows (FAILED + any
    residual DELIVERED that escaped delete-on-drain) and never touches
    PENDING or recent rows — bounding how long the transient queue can hold
    (redacted) user text (docs/encryption-at-rest-directive.md §7, PR-3)."""

    def _make(self, tenant, status, age_days):
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_x",
            payload={"message_text": "x", "user_param": "U_x", "user_timezone": "UTC"},
            user_text="x",
            delivery_status=status,
        )
        PendingMessage.objects.filter(id=msg.id).update(created_at=timezone.now() - timedelta(days=age_days))
        return msg

    def test_deletes_old_terminal_spares_recent_and_pending(self):
        from apps.router.pending_queue import cleanup_stale_pending_messages_task

        user = _make_user(line_user_id="U_sweep")
        tenant = _make_tenant(user)

        old_failed = self._make(tenant, PendingMessage.Status.FAILED, 20)
        old_delivered = self._make(tenant, PendingMessage.Status.DELIVERED, 20)
        recent_failed = self._make(tenant, PendingMessage.Status.FAILED, 3)
        old_pending = self._make(tenant, PendingMessage.Status.PENDING, 30)

        result = cleanup_stale_pending_messages_task()

        self.assertEqual(result["deleted"], 2)
        remaining = set(PendingMessage.objects.values_list("id", flat=True))
        self.assertNotIn(old_failed.id, remaining)
        self.assertNotIn(old_delivered.id, remaining)
        self.assertIn(recent_failed.id, remaining)
        # PENDING rows are owned by the drain / reaper — never swept here even
        # when ancient (the drain flips them to FAILED on the next tick).
        self.assertIn(old_pending.id, remaining)


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ProactiveContextIOSBridgeTest(TestCase):
    """The [earlier-from-you ...] continuity bridge on the iOS/app inbound path.

    Production failure (tenant 148ccf1c, iOS-only since 2026-06-25): the
    assistant sends a proactive cron question, the user replies from the iOS
    app, and the reply reaches the container with NO record of the question —
    because (1) the iOS inbound path never called ``surface_proactive_context``
    (only Telegram/LINE ingress did), and (2) rows were recorded under the
    OUTBOUND transport chosen by ``resolve_user_channel`` (telegram/line, since
    those links still exist), which an app-side channel-scoped lookup would
    never have matched. The fix surfaces TENANT-wide at the iOS drain — the
    single point where iOS content becomes container-bound.
    """

    def setUp(self):
        from apps.router.models import ChatThread

        # telegram_chat_id is still linked → the old code recorded proactive rows
        # under channel='telegram' even though the user now replies via the app.
        self.user = User.objects.create_user(
            username=f"iosbridge_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=99123,
            preferred_channel="telegram",
        )
        self.tenant = _make_tenant(self.user, container_fqdn="oc-iosbridge.example.com")
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)

    def _make_pending(self, client_msg_id: str, *, user_text: str):
        """A PENDING AppChatMessage + its iOS PendingMessage queue row, keyed by
        the thread (the coalesce key) — mirrors the real iOS ingress shape
        (``payload.message_text`` carries NO proactive block; the bridge is added
        only at the drain)."""
        from apps.router.models import AppChatMessage

        AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=client_msg_id,
            user_text=user_text,
            status=AppChatMessage.Status.PENDING,
        )
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(self.thread.id),
            payload={
                "message_text": user_text,
                "user_param": f"thread:{self.thread.id}",
                "user_timezone": "UTC",
                "client_msg_id": client_msg_id,
                "thread_id": str(self.thread.id),
            },
            user_text=user_text,
        )

    def _drain(self):
        return drain_pending_messages_for_tenant_task(str(self.tenant.id), "ios", str(self.thread.id))

    @staticmethod
    def _posted_content(mock_post):
        return mock_post.call_args.kwargs["json"]["messages"][0]["content"]

    @patch("apps.router.pending_queue.httpx.post")
    def test_telegram_recorded_row_surfaces_and_consumes_on_ios_reply(self, mock_post):
        # The EXACT production failure: outbound recorded channel='telegram',
        # reply arrives via the iOS app. Must surface AND consume.
        from apps.router.models import ProactiveOutbound
        from apps.router.proactive_context import record_proactive_outbound

        mock_post.return_value = _ok_chat_response("it went great")
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="99123",
            message_text="How did the Jasmine call go?",
            job_name="Personal Question",
        )
        assert row is not None
        self._make_pending("ios-1", user_text="It went great, thanks for asking")

        self._drain()

        content = self._posted_content(mock_post)
        self.assertIn("earlier-from-you", content)
        self.assertIn("How did the Jasmine call go?", content)
        # Consumed at the drain (the point the text became container-bound).
        row.refresh_from_db()
        self.assertIsNotNone(row.consumed_at)
        # Row still exists with its recorded transport for audit (only consumed).
        self.assertEqual(ProactiveOutbound.objects.get(id=row.id).channel, "telegram")

    @patch("apps.router.pending_queue.httpx.post")
    def test_block_prepended_once_at_top_of_singleton(self, mock_post):
        from apps.router.proactive_context import record_proactive_outbound

        mock_post.return_value = _ok_chat_response("ok")
        record_proactive_outbound(
            tenant=self.tenant, channel="app", channel_user_id="u-app", message_text="ping question body"
        )
        self._make_pending("ios-s", user_text="my reply text")

        self._drain()

        content = self._posted_content(mock_post)
        # Exactly once (never injected at ingress AND drain), at the top.
        self.assertEqual(content.count("earlier-from-you"), 1)
        self.assertLess(content.index("ping question body"), content.index("my reply text"))

    @patch("apps.router.pending_queue.httpx.post")
    def test_block_prepended_once_for_coalesced_batch(self, mock_post):
        from apps.router.proactive_context import record_proactive_outbound

        mock_post.return_value = _ok_chat_response("ok")
        record_proactive_outbound(
            tenant=self.tenant, channel="telegram", channel_user_id="99123", message_text="cron question body"
        )
        # Two rapid iOS messages for the same thread → coalesce into one OC turn.
        self._make_pending("ios-c1", user_text="reply one")
        self._make_pending("ios-c2", user_text="reply two")

        result = self._drain()
        self.assertEqual(result["batch_size"], 2)
        self.assertEqual(mock_post.call_count, 1)

        content = self._posted_content(mock_post)
        # Once at the top of the coalesced turn — NOT once per pending message.
        self.assertEqual(content.count("earlier-from-you"), 1)
        self.assertLess(content.index("cron question body"), content.index("reply one"))
        self.assertLess(content.index("reply one"), content.index("reply two"))

    @patch("apps.router.pending_queue.httpx.post")
    def test_consumed_row_not_resurfaced_on_separate_later_turn(self, mock_post):
        # Consumption idempotence: once consumed and past the 5-min follow-up
        # window, a later independent iOS turn does not re-surface the row.
        from apps.router.models import ProactiveOutbound
        from apps.router.proactive_context import record_proactive_outbound

        mock_post.return_value = _ok_chat_response("ok")
        row = record_proactive_outbound(
            tenant=self.tenant, channel="telegram", channel_user_id="99123", message_text="only once please"
        )
        assert row is not None
        self._make_pending("ios-first", user_text="first turn")
        self._drain()
        # Push consumption outside the 5-minute follow-up window.
        ProactiveOutbound.objects.filter(id=row.id).update(consumed_at=timezone.now() - timedelta(minutes=10))

        self._make_pending("ios-second", user_text="second turn")
        self._drain()

        content = self._posted_content(mock_post)
        self.assertNotIn("earlier-from-you", content)


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ProactiveContextIOSEndToEndTest(TestCase):
    """Full iOS ingress → drain, to prove a single enqueue→drain turn includes
    the bridge block exactly ONCE (injected at the drain only, never also at
    ingress). ``publish_task`` runs synchronously in tests (no QStash token), so
    the POST drives the container POST inline."""

    def setUp(self):
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(
            username=f"iose2e_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=99456,
            preferred_channel="telegram",
        )
        self.tenant = _make_tenant(self.user, container_fqdn="oc-iose2e.example.com")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_enqueue_then_drain_includes_block_exactly_once(self, mock_post, _mock_ner):
        from apps.router.proactive_context import record_proactive_outbound

        mock_post.return_value = _ok_chat_response("noted")
        record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="99456",
            message_text="did you finish the report?",
        )

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "yes, all done", "client_msg_id": "e2e-1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertEqual(content.count("earlier-from-you"), 1)
        self.assertIn("did you finish the report?", content)
        self.assertIn("yes, all done", content)


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ThreadRecapOnWakeTest(TestCase):
    """Deterministic conversation recap prepended to a cold iOS session.

    Production incident (2026-07-10): each iOS thread is its own OpenClaw
    session on an ephemeral EmptyDir; hibernation/restart wipes the session
    transcript. A user returned two days later ("just wanted to jump back into
    this fable 5 usage") in the SAME thread and the assistant answered "What's
    fable 5? Not ringing a bell." — the full history sat in ``app_chat_messages``
    the whole time. The recap re-anchors the agent when ``Tenant.last_wake_at``
    is newer than the thread's last delivered turn.

    ``_detect_pii`` is stubbed to [] so ``redact_user_message`` (which the recap
    runs over the verbatim ``user_text``) is deterministic; the redaction path
    itself is asserted in ``test_verbatim_user_text_is_redacted_not_leaked``.
    """

    def setUp(self):
        from apps.router.models import ChatThread

        self.user = User.objects.create_user(
            username=f"recap_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=91777,
            preferred_channel="telegram",
        )
        self.tenant = _make_tenant(self.user, container_fqdn="oc-recap.example.com")
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)

    # --- helpers -------------------------------------------------------------

    def _prior_turn(self, cid, *, user_text, reply_text, minutes_ago, thread=None):
        """A delivered (READY) AppChatMessage with explicit created_at/replied_at
        so ordering + the wake-vs-last-turn comparison are deterministic."""
        from apps.router.models import AppChatMessage

        thread = thread or self.thread
        ts = timezone.now() - timedelta(minutes=minutes_ago)
        m = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id=cid,
            user_text=user_text,
            reply_text=reply_text,
            status=AppChatMessage.Status.READY,
            replied_at=ts,
        )
        AppChatMessage.objects.filter(pk=m.pk).update(created_at=ts)
        return m

    def _queue_turn(self, cid, *, user_text, thread=None):
        """A PENDING AppChatMessage + its iOS PendingMessage queue row (the turn
        being delivered). Mirrors real iOS ingress: message_text carries NO
        recap — the bridge is added only at the drain."""
        from apps.router.models import AppChatMessage

        thread = thread or self.thread
        AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id=cid,
            user_text=user_text,
            status=AppChatMessage.Status.PENDING,
        )
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(thread.id),
            payload={
                "message_text": user_text,
                "user_param": f"thread:{thread.id}",
                "user_timezone": "UTC",
                "client_msg_id": cid,
                "thread_id": str(thread.id),
            },
            user_text=user_text,
        )

    def _set_wake(self, minutes_ago):
        Tenant.objects.filter(id=self.tenant.id).update(last_wake_at=timezone.now() - timedelta(minutes=minutes_ago))

    def _drain(self, thread=None):
        thread = thread or self.thread
        return drain_pending_messages_for_tenant_task(str(self.tenant.id), "ios", str(thread.id))

    @staticmethod
    def _posted_content(mock_post):
        return mock_post.call_args.kwargs["json"]["messages"][0]["content"]

    # --- tests ---------------------------------------------------------------

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_recap_injected_when_wake_after_last_turn(self, mock_post, _mock_ner):
        # The exact incident: prior thread turn about "fable 5", the container
        # wakes, the user jumps back in — the recap must carry the prior exchange.
        mock_post.return_value = _ok_chat_response("ok")
        self._prior_turn(
            "old-1",
            user_text="we were digging into fable 5 usage last week",
            reply_text="fable 5 is the model powering your assistant",
            minutes_ago=2880,  # two days ago
        )
        self._set_wake(minutes_ago=30)  # woke AFTER the last turn
        self._queue_turn("new-1", user_text="jump back into this fable 5 usage")

        self._drain()

        content = self._posted_content(mock_post)
        # Present exactly once, at the very top of the turn.
        self.assertEqual(content.count("conversation-recap"), 1)
        self.assertTrue(content.startswith("[conversation-recap"))
        # Carries the prior exchange (assistant reply verbatim; user text passes
        # through redaction unchanged here — no PII).
        self.assertIn("fable 5 is the model powering your assistant", content)
        self.assertIn("we were digging into fable 5 usage last week", content)
        # And precedes the user's new message.
        self.assertLess(content.index("conversation-recap"), content.index("jump back into this fable 5 usage"))

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_no_recap_when_warm_container(self, mock_post, _mock_ner):
        # Wake happened BEFORE the last delivered turn → transcript survived.
        mock_post.return_value = _ok_chat_response("ok")
        self._prior_turn("old-1", user_text="earlier stuff", reply_text="earlier reply", minutes_ago=10)
        self._set_wake(minutes_ago=45)  # woke BEFORE the last turn
        self._queue_turn("new-1", user_text="continuing normally")

        self._drain()

        self.assertNotIn("conversation-recap", self._posted_content(mock_post))

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_no_recap_when_never_woke(self, mock_post, _mock_ner):
        # last_wake_at is null (default) → nothing to rehydrate.
        mock_post.return_value = _ok_chat_response("ok")
        self._prior_turn("old-1", user_text="earlier stuff", reply_text="earlier reply", minutes_ago=10)
        self._queue_turn("new-1", user_text="continuing normally")

        self._drain()

        self.assertNotIn("conversation-recap", self._posted_content(mock_post))

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_first_ever_turn_no_history_no_crash(self, mock_post, _mock_ner):
        # No prior delivered turns in the thread. Woken, but nothing to recap.
        mock_post.return_value = _ok_chat_response("ok")
        self._set_wake(minutes_ago=5)
        self._queue_turn("new-1", user_text="hello for the first time")

        result = self._drain()

        self.assertEqual(result["delivered"], 1)
        self.assertNotIn("conversation-recap", self._posted_content(mock_post))

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    def test_truncation_respects_total_cap_and_drops_oldest(self, _mock_ner):
        # Long history: the total block is capped and the OLDEST exchanges are
        # dropped first (recency wins). Asserted at the builder for precision.
        from apps.router.thread_recap import RECAP_TOTAL_CHAR_CAP, build_thread_recap_block

        for i in range(8):
            self._prior_turn(
                f"old-{i}",
                user_text=f"umsg_{i} " + ("yak " * 120),
                reply_text=f"TOKEN_{i} " + ("cat " * 120),
                minutes_ago=2000 - i,  # i=0 oldest, i=7 newest
            )
        self.tenant.last_wake_at = timezone.now()

        recap = build_thread_recap_block(self.tenant, str(self.thread.id))

        self.assertTrue(recap.startswith("[conversation-recap"))
        self.assertLessEqual(len(recap), RECAP_TOTAL_CHAR_CAP)
        # Newest survives, oldest was dropped to fit.
        self.assertIn("TOKEN_7", recap)
        self.assertNotIn("TOKEN_0", recap)

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_empty_reply_sibling_rows_excluded(self, mock_post, _mock_ner):
        # A coalesced turn stores the combined reply on ONE representative row;
        # siblings are READY with empty reply_text. They must not appear.
        mock_post.return_value = _ok_chat_response("ok")
        self._prior_turn("rep", user_text="rep question", reply_text="the real combined answer", minutes_ago=60)
        self._prior_turn("sibling", user_text="SIBLING_UTEXT here", reply_text="", minutes_ago=60)
        self._set_wake(minutes_ago=20)
        self._queue_turn("new-1", user_text="back again")

        self._drain()

        content = self._posted_content(mock_post)
        self.assertIn("conversation-recap", content)
        self.assertIn("the real combined answer", content)
        self.assertNotIn("SIBLING_UTEXT", content)

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_recap_and_proactive_block_coexist(self, mock_post, _mock_ner):
        # Both continuity blocks in one turn: order must be recap → proactive →
        # user text (recap = oldest context, then the recent proactive send).
        from apps.router.proactive_context import record_proactive_outbound

        mock_post.return_value = _ok_chat_response("ok")
        self._prior_turn("old-1", user_text="prior question", reply_text="prior assistant answer", minutes_ago=3000)
        record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="91777",
            message_text="did the meeting happen?",
        )
        self._set_wake(minutes_ago=15)
        self._queue_turn("new-1", user_text="my fresh reply now")

        self._drain()

        content = self._posted_content(mock_post)
        self.assertEqual(content.count("conversation-recap"), 1)
        self.assertEqual(content.count("earlier-from-you"), 1)
        recap_idx = content.index("conversation-recap")
        proactive_idx = content.index("earlier-from-you")
        user_idx = content.index("my fresh reply now")
        self.assertLess(recap_idx, proactive_idx)
        self.assertLess(proactive_idx, user_idx)

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_recap_is_thread_isolated(self, mock_post, _mock_ner):
        from apps.router.models import ChatThread

        mock_post.return_value = _ok_chat_response("ok")
        other = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=False)
        self._prior_turn("main-old", user_text="main q", reply_text="MAINTHREAD_REPLY", minutes_ago=100)
        self._prior_turn(
            "other-old", user_text="OTHER_UTEXT", reply_text="OTHERTHREAD_REPLY", minutes_ago=100, thread=other
        )
        self._set_wake(minutes_ago=20)
        self._queue_turn("new-1", user_text="back in the main thread")

        self._drain()

        content = self._posted_content(mock_post)
        self.assertIn("MAINTHREAD_REPLY", content)
        self.assertNotIn("OTHERTHREAD_REPLY", content)
        self.assertNotIn("OTHER_UTEXT", content)

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_exactly_once_across_consecutive_turns(self, mock_post, _mock_ner):
        # First post-wake turn gets the recap; once it is delivered its
        # replied_at moves last_turn past last_wake_at, so the next turn does not.
        mock_post.return_value = _ok_chat_response("ok")
        self._prior_turn("old-1", user_text="original topic", reply_text="original answer", minutes_ago=3000)
        self._set_wake(minutes_ago=30)

        self._queue_turn("turn-1", user_text="first reply after wake")
        self._drain()
        self.assertIn("conversation-recap", self._posted_content(mock_post))

        self._queue_turn("turn-2", user_text="second reply after wake")
        self._drain()
        self.assertNotIn("conversation-recap", self._posted_content(mock_post))

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    def test_builder_is_idempotent_pure_read(self, _mock_ner):
        # Drain retries recompute the same condition. The builder is a pure read
        # (no consumption / mutation), so repeated calls return identical content.
        from apps.router.thread_recap import build_thread_recap_block

        self._prior_turn("old-1", user_text="topic", reply_text="answer", minutes_ago=500)
        self.tenant.last_wake_at = timezone.now()

        first = build_thread_recap_block(self.tenant, str(self.thread.id))
        second = build_thread_recap_block(self.tenant, str(self.thread.id))

        self.assertTrue(first)
        self.assertEqual(first, second)

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_verbatim_user_text_is_redacted_not_leaked(self, mock_post, _mock_ner):
        # AppChatMessage.user_text is stored VERBATIM (real values) for the
        # owner-facing ?since= feed. The recap must NOT leak that to the model —
        # it re-runs the same redaction seam the live turn used. Seed a known
        # binding so the map pass (pre-NER) substitutes it deterministically.
        mock_post.return_value = _ok_chat_response("ok")
        self.tenant.pii_entity_map = {"[PERSON_5]": "Priya"}
        self.tenant.save(update_fields=["pii_entity_map"])
        self._prior_turn(
            "old-1",
            user_text="the call with Priya went great",
            reply_text="glad the [PERSON_5] call went well",
            minutes_ago=200,
        )
        self._set_wake(minutes_ago=20)
        self._queue_turn("new-1", user_text="picking this back up")

        self._drain()

        content = self._posted_content(mock_post)
        self.assertIn("conversation-recap", content)
        self.assertNotIn("the call with Priya went great", content)
        self.assertNotIn("Priya", content)
        self.assertRegex(content, r"\[PERSON_5(\|[^\]]*)?\]")


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class ReplyArtifactPersistenceTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Sarah"}}
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal", "pii_entity_map"])
        self.thread = ChatThread.objects.create(
            tenant=self.tenant,
            user=self.user,
            title="Artifacts",
            is_main=True,
        )

    @staticmethod
    def _table(rows=26):
        lines = ["| Name | Value |", "| --- | --- |"]
        lines.extend(f"| row {index} | [PERSON_1] |" for index in range(rows))
        return "\n".join(lines)

    def _batch(self, *client_ids):
        batch = []
        for client_id in client_ids:
            AppChatMessage.objects.create(
                tenant=self.tenant,
                user=self.user,
                thread=self.thread,
                client_msg_id=client_id,
                user_text=f"question {client_id}",
            )
            batch.append(
                PendingMessage(
                    tenant=self.tenant,
                    channel=PendingMessage.Channel.IOS,
                    channel_user_id=str(self.thread.id),
                    payload={"client_msg_id": client_id},
                    user_text=f"question {client_id}",
                )
            )
        return batch

    @patch("apps.router.pending_queue._dispatch_push")
    def test_move_precedes_clamp_and_generated_chip_lands_on_representative_row(self, _push):
        from apps.journal.models import Document
        from apps.router.pending_queue import _store_ios_turn_reply

        batch = self._batch("sibling", "representative")
        reply = self._table() + "\n\n" + ("afterword " * 2200)
        _store_ios_turn_reply(self.tenant, batch, reply)

        representative = AppChatMessage.objects.get(client_msg_id="representative")
        sibling = AppChatMessage.objects.get(client_msg_id="sibling")
        self.assertEqual(representative.status, AppChatMessage.Status.READY)
        self.assertEqual(len(representative.reply_text), 16000)
        self.assertIn("Saved the full table (26 rows)", representative.reply_text)
        self.assertIn("| Name | Value |", representative.reply_text)
        self.assertIn("| row 2 | [PERSON_1] |", representative.reply_text)
        self.assertNotIn("| row 3 | [PERSON_1] |", representative.reply_text)
        self.assertEqual(representative.journal_link["kind"], "project")
        self.assertEqual(sibling.status, AppChatMessage.Status.READY)
        self.assertEqual(sibling.reply_text, "")
        doc = Document.objects.get(
            tenant=self.tenant,
            kind="project",
            slug=representative.journal_link["slug"],
        )
        self.assertGreater(len(doc.markdown), 16000)
        self.assertIn("| Name | Value |", doc.markdown)

    @patch("apps.insights.markers.extract_and_record_insights")
    @patch("apps.router.pending_queue._dispatch_push")
    def test_quick_replies_and_redactions_survive_while_markers_stay_out_of_artifact(self, _push, mock_insights):
        from apps.journal.models import Document
        from apps.router.pending_queue import _store_ios_turn_reply

        mock_insights.side_effect = lambda text, **_kwargs: text
        batch = self._batch("markers")
        reply = (
            "[[insight:mood]]Pattern for [PERSON_1][[/insight]]\n"
            "[[chart:bar|secret]] MEDIA:https://example.test/file\n"
            "![remote](https://example.test/beacon.png)\n\n"
            + self._table()
            + "\n[[journal-link: project|unrelated|Other]]"
            + "\n[[quick-replies: Open Journal | Later]]"
        )
        _store_ios_turn_reply(self.tenant, batch, reply)

        row = AppChatMessage.objects.get(client_msg_id="markers")
        self.assertEqual(row.quick_replies, ["Open Journal", "Later"])
        self.assertEqual(
            row.reply_redactions,
            [{"placeholder": "[PERSON_1]", "value": "Sarah"}],
        )
        self.assertNotEqual(row.journal_link["slug"], "unrelated")
        doc = Document.objects.get(tenant=self.tenant, slug=row.journal_link["slug"])
        for marker in ("[[insight:", "[[chart:", "MEDIA:", "[[journal-link:", "[[quick-replies:"):
            self.assertNotIn(marker, doc.markdown)
        self.assertIn("Pattern for [PERSON_1]", doc.markdown)
        self.assertNotIn("![remote]", doc.markdown)
        self.assertIn("[remote](https://example.test/beacon.png)", doc.markdown)

    @patch("apps.journal.reply_artifacts.upsert_reply_artifact", side_effect=RuntimeError("db down"))
    @patch("apps.router.pending_queue._dispatch_push")
    def test_journal_failure_falls_back_to_clamped_inline_reply(self, _push, _artifact_write):
        from apps.journal.models import Document
        from apps.router.pending_queue import _store_ios_turn_reply

        batch = self._batch("fallback")
        reply = self._table() + ("x" * 17000)
        _store_ios_turn_reply(self.tenant, batch, reply)

        row = AppChatMessage.objects.get(client_msg_id="fallback")
        self.assertEqual(len(row.reply_text), 16000)
        self.assertIn("| Name | Value |", row.reply_text)
        self.assertIsNone(row.journal_link)
        self.assertFalse(Document.objects.filter(tenant=self.tenant).exists())

    @patch("apps.router.pending_queue._dispatch_push")
    def test_existing_link_with_selected_table_is_reused(self, _push):
        from apps.journal.models import Document
        from apps.router.pending_queue import _store_ios_turn_reply

        table = self._table()
        linked = Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.PROJECT,
            slug="existing-table",
            title="Existing report",
            markdown=table,
        )
        batch = self._batch("reuse-link")
        reply = table + "\n[[journal-link: project|existing-table|Existing report]]"

        _store_ios_turn_reply(self.tenant, batch, reply)

        row = AppChatMessage.objects.get(client_msg_id="reuse-link")
        self.assertEqual(row.journal_link["slug"], linked.slug)
        self.assertIn("Saved the full table (26 rows)", row.reply_text)
        self.assertIn("| row 2 | [PERSON_1] |", row.reply_text)
        self.assertNotIn("| row 3 | [PERSON_1] |", row.reply_text)
        self.assertEqual(Document.objects.filter(tenant=self.tenant).count(), 1)

    @patch("apps.router.pending_queue._dispatch_push")
    def test_under_threshold_table_stays_inline_without_journal_link(self, _push):
        from apps.journal.models import Document
        from apps.router.pending_queue import _store_ios_turn_reply

        table = self._table(rows=25)
        batch = self._batch("under-threshold")

        _store_ios_turn_reply(self.tenant, batch, table)

        row = AppChatMessage.objects.get(client_msg_id="under-threshold")
        self.assertEqual(row.reply_text, table)
        self.assertIsNone(row.journal_link)
        self.assertFalse(Document.objects.filter(tenant=self.tenant).exists())

    @patch("apps.router.pending_queue._dispatch_push")
    def test_repeat_persistence_with_same_client_id_converges_on_one_document(self, _push):
        from apps.journal.models import Document
        from apps.router.pending_queue import _store_ios_turn_reply

        batch = self._batch("retry-key")
        _store_ios_turn_reply(self.tenant, batch, self._table())
        _store_ios_turn_reply(self.tenant, batch, self._table())
        self.assertEqual(Document.objects.filter(tenant=self.tenant).count(), 1)

    @patch("apps.router.pending_queue._dispatch_push")
    def test_final_chat_update_failure_rolls_back_artifact(self, _push):
        from django.db.models.query import QuerySet

        from apps.journal.models import Document
        from apps.router.pending_queue import _store_ios_turn_reply

        batch = self._batch("chat-fails")
        real_update = QuerySet.update

        def fail_app_update(queryset, **kwargs):
            if queryset.model is AppChatMessage and "reply_text" in kwargs:
                raise RuntimeError("forced final chat update failure")
            return real_update(queryset, **kwargs)

        with (
            patch.object(QuerySet, "update", new=fail_app_update),
            self.assertRaisesRegex(RuntimeError, "forced final chat update failure"),
        ):
            _store_ios_turn_reply(self.tenant, batch, self._table())

        self.assertFalse(Document.objects.filter(tenant=self.tenant).exists())
        row = AppChatMessage.objects.get(client_msg_id="chat-fails")
        self.assertEqual(row.status, AppChatMessage.Status.PENDING)
