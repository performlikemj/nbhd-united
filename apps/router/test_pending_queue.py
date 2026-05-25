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
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.router.models import PendingMessage
from apps.router.pending_queue import (
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
        # publish_task in tests has no QStash -> sync-fallback drain.
        # The drain ran inline so the row should now be delivered.
        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.DELIVERED)
        self.assertIsNone(msg.delivery_in_flight_until)
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

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_concurrent_drain_skips_message_with_live_lease(self, mock_post, _mock_send):
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

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.DELIVERED)
        self.assertIsNone(msg.delivery_in_flight_until)

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
        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.DELIVERED)
        self.assertIsNone(msg.delivery_in_flight_until)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class PendingMessageOrderingTest(TestCase):
    """Multiple messages for the same key drain in FIFO order."""

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("apps.router.pending_queue.httpx.post")
    def test_two_messages_drain_in_arrival_order(self, mock_post, _mock_send):
        """Two messages enqueued back-to-back must arrive at the
        container in FIFO order."""
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

        # First drain — should pick up m1, then re-schedule itself (sync
        # fallback) and deliver m2 in the same call chain.
        result1 = drain_pending_messages_for_tenant_task(str(tenant.id), "line", "U_order")
        self.assertEqual(result1["delivered"], 1)

        m1.refresh_from_db()
        m2.refresh_from_db()
        self.assertEqual(m1.delivery_status, PendingMessage.Status.DELIVERED)
        self.assertEqual(m2.delivery_status, PendingMessage.Status.DELIVERED)

        # Two POSTs total, in arrival order. ``call_args`` reflects the
        # last call (m2's "second"), so check ``call_args_list[0]`` for
        # the FIFO head.
        self.assertEqual(mock_post.call_count, 2)
        first_payload = mock_post.call_args_list[0].kwargs["json"]
        self.assertEqual(first_payload["messages"][0]["content"], "first")
        second_payload = mock_post.call_args_list[1].kwargs["json"]
        self.assertEqual(second_payload["messages"][0]["content"], "second")


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

        b_row.refresh_from_db()
        self.assertEqual(b_row.delivery_status, PendingMessage.Status.DELIVERED)
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

        fresh.refresh_from_db()
        self.assertEqual(fresh.delivery_status, PendingMessage.Status.DELIVERED)
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
        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.DELIVERED)

        # At least one POST went to /v1/chat/completions and at least
        # one went to the Telegram Bot API for the reply delivery.
        urls = [call.args[0] for call in mock_post.call_args_list]
        self.assertTrue(any("/v1/chat/completions" in u for u in urls))
        self.assertTrue(any("api.telegram.org" in u for u in urls))


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
        msg.refresh_from_db()
        self.assertEqual(msg.delivery_status, PendingMessage.Status.DELIVERED)
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
