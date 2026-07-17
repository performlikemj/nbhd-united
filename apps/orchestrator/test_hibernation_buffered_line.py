"""Regression: hibernation buffered LINE delivery must reuse the live
webhook formatter so markdown is stripped and Flex bubbles are used.
Also covers the resilience semantics (per-message attempt cap, transient
5xx retry, head-of-line preservation, dropped-message apology) added
after the 2026-04-28 incident."""

from __future__ import annotations

import json
import secrets
from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase, override_settings

from apps.router.models import BufferedMessage
from apps.tenants.models import Tenant, User


def _make_user(line_user_id: str) -> User:
    return User.objects.create_user(
        username=f"hib_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        line_user_id=line_user_id,
        preferred_channel="line",
    )


def _make_tenant(user: User) -> Tenant:
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-test.example.com",
    )


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class DeliverBufferedLineFormattingTest(TestCase):
    """Buffered LINE replies must go through the same Flex/strip pipeline
    as the live webhook (regression for raw markdown leaking into LINE)."""

    @patch("apps.router.line_webhook._send_line_messages")
    @patch("httpx.post")
    def test_buffered_line_delivery_uses_flex_pipeline(self, mock_post, mock_send):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        mock_send.return_value = True

        user = _make_user(line_user_id="U_buffered_md")
        tenant = _make_tenant(user)

        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="What's a good leg workout?",
        )

        # Container returns long markdown content (the kind that previously
        # leaked into LINE as raw asterisks via _send_line_text).
        ai_text = (
            "## Leg Day\n"
            "1. **Squats** — 4 sets x 8 reps\n"
            "   *Keep your back straight*\n"
            "2. **Walking Lunges** — 3 sets x 10 each leg\n"
            "   *Hold dumbbells at your sides*\n"
            "3. **Leg Press** — 3 sets x 10-12 reps\n"
            "   *Go deep*\n"
            "---\n"
            "Rest **60-90 seconds** between sets."
        )
        container_resp = MagicMock()
        container_resp.is_success = True
        container_resp.status_code = 200
        container_resp.json.return_value = {
            "choices": [{"message": {"content": ai_text}}],
            "usage": {},
            "model": "test",
        }
        container_resp.raise_for_status = MagicMock()
        mock_post.return_value = container_resp

        result = deliver_buffered_messages_task(str(tenant.id))

        self.assertEqual(result["delivered"], 1)
        mock_send.assert_called_once()
        line_user_id, messages = mock_send.call_args[0][:2]
        self.assertEqual(line_user_id, "U_buffered_md")
        self.assertGreaterEqual(len(messages), 1)

        # Either Flex bubble (preferred) or plain text — but never raw markdown.
        first = messages[0]
        if first["type"] == "text":
            self.assertNotIn("**", first["text"])
            self.assertNotIn("---", first["text"])
        else:
            self.assertEqual(first["type"], "flex")
            payload = json.dumps(first)
            # Asterisks should be stripped from text components throughout
            # the bubble. (alt_text is also derived via _strip_markdown.)
            self.assertNotIn("**", payload)

        # Reply API not used for buffered delivery — token would be expired.
        self.assertIsNone(mock_send.call_args.kwargs.get("reply_token"))

    @patch("apps.router.line_webhook._send_line_messages")
    @patch("httpx.post")
    def test_empty_ai_response_does_not_send(self, mock_post, mock_send):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = _make_user(line_user_id="U_buffered_empty")
        tenant = _make_tenant(user)
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="hi",
        )

        container_resp = MagicMock()
        container_resp.is_success = True
        container_resp.status_code = 200
        container_resp.json.return_value = {
            "choices": [{"message": {"content": ""}}],
            "usage": {},
            "model": "test",
        }
        container_resp.raise_for_status = MagicMock()
        mock_post.return_value = container_resp

        deliver_buffered_messages_task(str(tenant.id))
        mock_send.assert_not_called()


class DeliverBufferedEvalSinkIsolationTest(TestCase):
    @patch("apps.router.line_webhook.relay_ai_response_to_line")
    @patch("apps.router.pending_queue.relay_ai_response_to_telegram")
    @patch("httpx.post")
    def test_eval_sink_telegram_and_line_rows_are_terminal_without_transport(
        self,
        mock_post,
        telegram_relay,
        line_relay,
    ):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = User.objects.create_user(
            username=f"hib_eval_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=778811,
            line_user_id="U_hib_eval",
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-eval.example.com",
            is_eval_sink=True,
        )
        telegram_row = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.TELEGRAM,
            payload={"update_id": 1},
            user_text="telegram question",
        )
        line_row = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="line question",
        )

        result = deliver_buffered_messages_task(str(tenant.id))

        self.assertEqual(result, {"delivered": 0, "failed": 0, "dropped": 2, "skipped_in_flight": 0})
        mock_post.assert_not_called()
        telegram_relay.assert_not_called()
        line_relay.assert_not_called()
        for row in (telegram_row, line_row):
            row.refresh_from_db()
            self.assertTrue(row.delivered)
            self.assertIsNotNone(row.delivered_at)
            self.assertEqual(row.delivery_status, BufferedMessage.Status.FAILED)
            self.assertIsNone(row.delivery_in_flight_until)


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


def _five_hundred_response():
    resp = MagicMock()
    resp.status_code = 502
    resp.is_success = False
    resp.json.return_value = {}
    resp.raise_for_status = MagicMock()
    return resp


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class DeliverBufferedResilienceTest(TestCase):
    """Per-message attempt cap, transient-retry, and head-of-line semantics
    added after the 2026-04-28 incident where a single slow chat completion
    wedged the queue forever via QStash retry-from-head."""

    @patch("apps.orchestrator.hibernation.time.sleep", return_value=None)
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_transient_5xx_retried_then_succeeds_without_attempt_increment(self, mock_post, _mock_send, _mock_sleep):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        # First response is 502 (cold container), second is 200.
        mock_post.side_effect = [_five_hundred_response(), _ok_chat_response("welcome back!")]

        user = _make_user(line_user_id="U_resilience_5xx")
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="hi",
        )

        result = deliver_buffered_messages_task(str(tenant.id))

        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(mock_post.call_count, 2)

        # Delivered after a transient retry → hard-deleted on confirmed forward
        # (PR-3 privacy sweep). Deletion is the proof the message DELIVERED
        # rather than being dropped: the transient retry never burned the
        # attempt cap (that path would have left a FAILED row instead).
        self.assertFalse(BufferedMessage.objects.filter(id=msg.id).exists())

    @patch("apps.orchestrator.hibernation.time.sleep", return_value=None)
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_persistent_5xx_increments_attempts_and_breaks_loop(self, mock_post, mock_send, _mock_sleep):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        # Every retry attempt sees 502 — message stays undelivered.
        mock_post.return_value = _five_hundred_response()

        user = _make_user(line_user_id="U_resilience_persistent_5xx")
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="hi",
        )

        with self.assertRaises(RuntimeError):
            deliver_buffered_messages_task(str(tenant.id))

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_attempts, 1)
        self.assertFalse(msg.delivered)
        # No LINE Push was sent because no successful chat completion.
        mock_send.assert_not_called()

    @patch("apps.orchestrator.hibernation._send_apology_for_dropped_message")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_message_past_attempt_cap_dropped_with_apology(self, mock_post, _mock_send, mock_apology):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = _make_user(line_user_id="U_resilience_drop")
        tenant = _make_tenant(user)
        stuck = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="this one keeps timing out",
            delivery_attempts=3,  # already at cap
        )

        result = deliver_buffered_messages_task(str(tenant.id))

        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["delivered"], 0)
        # Container was NOT contacted for the dropped message.
        mock_post.assert_not_called()
        mock_apology.assert_called_once()
        called_msg = mock_apology.call_args[0][1]
        self.assertEqual(called_msg.id, stuck.id)

        stuck.refresh_from_db()
        self.assertTrue(stuck.delivered)
        self.assertEqual(stuck.delivery_status, BufferedMessage.Status.FAILED)

    @patch("apps.orchestrator.hibernation._send_apology_for_dropped_message")
    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_dropped_head_does_not_block_fresh_messages_behind_it(self, mock_post, mock_send, _mock_apology):
        """Regression for the 2026-04-28 head-of-line stall: a maxed-out
        message at the head of the queue must NOT prevent a fresh message
        behind it from being delivered."""
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        mock_post.return_value = _ok_chat_response("here's the answer")

        user = _make_user(line_user_id="U_resilience_head")
        tenant = _make_tenant(user)
        # Older stuck message (at cap) → should be dropped.
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="old stuck message",
            delivery_attempts=3,
        )
        # Newer fresh message → should be delivered in the same task run.
        fresh = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="please respond to me",
        )

        result = deliver_buffered_messages_task(str(tenant.id))

        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["delivered"], 1)
        # Fresh message was actually pushed.
        mock_send.assert_called_once()
        # Fresh message delivered → hard-deleted on confirmed forward (PR-3).
        self.assertFalse(BufferedMessage.objects.filter(id=fresh.id).exists())


class ApologyHelperTest(TestCase):
    @patch("apps.router.line_webhook._send_line_text", return_value=True)
    def test_eval_sink_dropped_buffer_apology_is_suppressed(self, mock_send_text):
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message

        user = _make_user(line_user_id="U_eval_buffer_apology")
        tenant = _make_tenant(user)
        tenant.is_eval_sink = True
        tenant.save(update_fields=["is_eval_sink", "updated_at"])
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={},
            user_text="failed question",
        )

        _send_apology_for_dropped_message(tenant, msg)

        mock_send_text.assert_not_called()

    @patch("apps.router.line_webhook._send_line_text", return_value=True)
    def test_apology_quotes_user_message_excerpt(self, mock_send_text):
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message

        user = _make_user(line_user_id="U_apology")
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="today's scale measured 68.3 when i woke up. add that to log",
        )

        _send_apology_for_dropped_message(tenant, msg)

        mock_send_text.assert_called_once()
        line_user_id, body = mock_send_text.call_args[0]
        self.assertEqual(line_user_id, "U_apology")
        # Apology mentions the message excerpt so the user knows what to re-send.
        self.assertIn("today's scale measured", body)
        # Doesn't try to look like the assistant.
        self.assertIn("Sorry", body)

    @patch("apps.router.line_webhook._send_line_text")
    def test_apology_swallows_line_push_failure(self, mock_send_text):
        """Failure of the apology push must not crash the delivery loop —
        otherwise the apology becomes a NEW way to wedge the queue."""
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message

        mock_send_text.side_effect = httpx.HTTPError("LINE API down")

        user = _make_user(line_user_id="U_apology_fail")
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="anything",
        )

        # Should NOT raise.
        _send_apology_for_dropped_message(tenant, msg)
        mock_send_text.assert_called_once()

    @patch("apps.router.line_webhook._send_line_text", return_value=True)
    def test_apology_localized_to_user_language(self, mock_send_text):
        """Apology must respect tenant.user.language. Falls back to English
        for languages without a translated key, but for languages we DO
        translate (en, ja) the user gets their language."""
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message

        user = _make_user(line_user_id="U_apology_ja")
        user.language = "ja"
        user.save(update_fields=["language"])
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="\u4f53\u91cd\u3092\u8a18\u9332",  # "log my weight" in JP
        )

        _send_apology_for_dropped_message(tenant, msg)

        mock_send_text.assert_called_once()
        body = mock_send_text.call_args[0][1]
        # English markers must NOT appear when ja translation exists.
        self.assertNotIn("Sorry", body)
        self.assertNotIn("It started with", body)
        # Japanese marker must appear.
        self.assertIn("\u3054\u3081\u3093\u306a\u3055\u3044", body)  # "Sorry" in JP
        # Excerpt is preserved (Unicode passes through .format()).
        self.assertIn("\u4f53\u91cd", body)

    @patch("apps.router.line_webhook._send_line_text", return_value=True)
    def test_apology_falls_back_to_english_for_untranslated_language(self, mock_send_text):
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message

        user = _make_user(line_user_id="U_apology_xx")
        user.language = "vi"  # Vietnamese — not translated yet, falls back
        user.save(update_fields=["language"])
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="hello",
        )

        _send_apology_for_dropped_message(tenant, msg)
        body = mock_send_text.call_args[0][1]
        self.assertIn("Sorry", body)  # English fallback

    @patch("apps.router.line_webhook._send_line_text", return_value=True)
    def test_apology_excerpt_strips_internal_framing(self, mock_send_text):
        """Belt-and-suspenders: if a BufferedMessage's ``user_text`` somehow
        contains agent-only framing (``[System: just updated…]`` etc.), the
        apology must strip it before quoting back to the user."""
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message

        user = _make_user(line_user_id="U_apology_strip")
        tenant = _make_tenant(user)
        pending_text = "log today's run, 5k in 24:30"
        framed = f"[System: just updated. User's message from before the update:]\n{pending_text}"
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text=framed,
        )

        _send_apology_for_dropped_message(tenant, msg)
        body = mock_send_text.call_args[0][1]
        self.assertNotIn("[System:", body)
        self.assertNotIn("User's message from before", body)
        self.assertIn("log today's run", body)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class DeliverBufferedInFlightLockTest(TestCase):
    """Per-row in-flight lease must prevent a concurrent QStash retry of
    ``deliver_buffered_messages_task`` from re-firing the chat completion
    while the first attempt is still mid-POST.

    Regression for the 2026-05-02 BYO Claude retry-storm incident on
    tenant 148ccf1c, where 5+ ``cli exec`` invocations fired for a
    single LINE prompt because the slow Claude turn timed out at 120s
    and QStash retried while the original CLI session was still running
    — the OpenClaw claude-cli backend rejects concurrent turns and falls
    back off to MiniMax."""

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_concurrent_invocation_skips_message_with_live_lease(self, mock_post, _mock_send):
        """While the first task call is mid-POST, a second concurrent call
        must observe the live lease and skip the row instead of firing a
        duplicate ``/v1/chat/completions``."""
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = _make_user(line_user_id="U_in_flight")
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="please reply once",
        )

        second_call_result: dict = {}

        def _slow_post(*args, **kwargs):
            # Mid-POST a second QStash retry fires. It must observe the
            # in-flight lease and skip the row instead of firing a
            # duplicate /v1/chat/completions at the container.
            second_call_result["data"] = deliver_buffered_messages_task(str(tenant.id))
            return _ok_chat_response("here you go")

        mock_post.side_effect = _slow_post

        result = deliver_buffered_messages_task(str(tenant.id))

        # First invocation delivered the message.
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["failed"], 0)
        # Second (concurrent) invocation saw the lease and skipped.
        self.assertEqual(second_call_result["data"]["delivered"], 0)
        self.assertEqual(second_call_result["data"]["failed"], 0)
        self.assertEqual(second_call_result["data"]["skipped_in_flight"], 1)
        # Crucially: only ONE chat completion was POSTed for the message.
        self.assertEqual(mock_post.call_count, 1)

        # Delivered → hard-deleted on confirmed forward (PR-3). The concurrent
        # retry observed the live lease and skipped, so only one delivery
        # happened and exactly one row was removed.
        self.assertFalse(BufferedMessage.objects.filter(id=msg.id).exists())

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_expired_lease_is_reclaimed_on_next_run(self, mock_post, _mock_send):
        """If a previous worker died after taking the lease but before
        clearing it, the next run (after the lease window) must reclaim
        the row. Otherwise stuck rows would block forever."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = _make_user(line_user_id="U_stale_lease")
        tenant = _make_tenant(user)
        # Simulate a stale lease that elapsed 30 minutes ago.
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="hi",
            delivery_in_flight_until=timezone.now() - timedelta(minutes=30),
        )
        mock_post.return_value = _ok_chat_response("ok")

        result = deliver_buffered_messages_task(str(tenant.id))

        self.assertEqual(result["delivered"], 1)
        # Reclaimed after an expired lease and delivered → hard-deleted (PR-3).
        self.assertFalse(BufferedMessage.objects.filter(id=msg.id).exists())

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_lease_cleared_on_failure_so_retry_can_reclaim(self, mock_post, _mock_send):
        """On a real per-message failure the lease must be cleared so the
        QStash retry can re-claim the row immediately rather than wait
        for the lease to expire on its own."""
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = _make_user(line_user_id="U_fail_clears_lease")
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="hi",
        )

        with (
            patch(
                "apps.orchestrator.hibernation._post_chat_completion_with_backoff",
                side_effect=RuntimeError("boom"),
            ),
            self.assertRaises(RuntimeError),
        ):
            deliver_buffered_messages_task(str(tenant.id))

        msg.refresh_from_db()
        self.assertEqual(msg.delivery_attempts, 1)
        self.assertIsNone(msg.delivery_in_flight_until)
        self.assertFalse(msg.delivered)


class ResolveChatTimeoutTest(TestCase):
    """BYO Claude (anthropic/* via the CLI backend) and reasoning models
    get the longer ``REASONING_MODEL_TIMEOUT`` so the cold-start +
    first-turn-with-full-agent-context latency doesn't trigger the
    short-timeout retry storm that the 2026-05-02 incident exposed."""

    def test_byo_anthropic_sonnet_uses_reasoning_timeout(self):
        from apps.billing.constants import (
            ANTHROPIC_SONNET_MODEL,
            REASONING_MODEL_TIMEOUT,
        )
        from apps.orchestrator.hibernation import _resolve_chat_timeout

        user = _make_user(line_user_id="U_sonnet")
        tenant = _make_tenant(user)
        tenant.preferred_model = ANTHROPIC_SONNET_MODEL
        tenant.save(update_fields=["preferred_model"])

        self.assertEqual(_resolve_chat_timeout(tenant), REASONING_MODEL_TIMEOUT)

    def test_byo_anthropic_opus_uses_reasoning_timeout(self):
        from apps.billing.constants import (
            ANTHROPIC_OPUS_MODEL,
            REASONING_MODEL_TIMEOUT,
        )
        from apps.orchestrator.hibernation import _resolve_chat_timeout

        user = _make_user(line_user_id="U_opus")
        tenant = _make_tenant(user)
        tenant.preferred_model = ANTHROPIC_OPUS_MODEL
        tenant.save(update_fields=["preferred_model"])

        self.assertEqual(_resolve_chat_timeout(tenant), REASONING_MODEL_TIMEOUT)

    def test_default_minimax_keeps_default_timeout(self):
        from apps.billing.constants import DEFAULT_CHAT_TIMEOUT, MINIMAX_MODEL
        from apps.orchestrator.hibernation import _resolve_chat_timeout

        user = _make_user(line_user_id="U_minimax")
        tenant = _make_tenant(user)
        tenant.preferred_model = MINIMAX_MODEL
        tenant.save(update_fields=["preferred_model"])

        self.assertEqual(_resolve_chat_timeout(tenant), DEFAULT_CHAT_TIMEOUT)

    def test_empty_preferred_model_keeps_default_timeout(self):
        from apps.billing.constants import DEFAULT_CHAT_TIMEOUT
        from apps.orchestrator.hibernation import _resolve_chat_timeout

        user = _make_user(line_user_id="U_unset")
        tenant = _make_tenant(user)
        # preferred_model unset (default)
        self.assertEqual(_resolve_chat_timeout(tenant), DEFAULT_CHAT_TIMEOUT)

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_byo_tenant_post_uses_longer_timeout(self, mock_post, _mock_send):
        """End-to-end: a BYO Claude tenant's buffered delivery must call
        ``_post_chat_completion_with_backoff`` with the BYO timeout."""
        from apps.billing.constants import (
            ANTHROPIC_SONNET_MODEL,
            REASONING_MODEL_TIMEOUT,
        )
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        mock_post.return_value = _ok_chat_response("hi")

        user = _make_user(line_user_id="U_byo_e2e")
        tenant = _make_tenant(user)
        tenant.preferred_model = ANTHROPIC_SONNET_MODEL
        tenant.save(update_fields=["preferred_model"])
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="hi",
        )

        deliver_buffered_messages_task(str(tenant.id))

        mock_post.assert_called_once()
        # Timeout kwarg passed through to httpx.post by the backoff helper.
        self.assertEqual(mock_post.call_args.kwargs["timeout"], REASONING_MODEL_TIMEOUT)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
)
class DeliverBufferedLineColdStartCoalesceTest(TestCase):
    """Hibernated LINE delivery coalesces N buffered messages into one
    OC turn (cold-start coalesce). Mirrors the warm-tenant coalesce in
    ``apps/router/pending_queue`` so the user gets one coherent reply
    after the ~45s wake instead of N separate replies (potentially N
    near-identical "you there?" pings)."""

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_three_buffered_line_messages_coalesce_into_one_post(self, mock_post, _mock_send):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        mock_post.return_value = _ok_chat_response("ack")

        user = _make_user(line_user_id="U_coalesce")
        tenant = _make_tenant(user)
        texts = ["please check fuel", "actually yesterday too", "you there?"]
        for txt in texts:
            BufferedMessage.objects.create(
                tenant=tenant,
                channel=BufferedMessage.Channel.LINE,
                payload={"events": []},
                user_text=txt,
            )

        result = deliver_buffered_messages_task(str(tenant.id))
        self.assertEqual(result["delivered"], 3)
        self.assertEqual(mock_post.call_count, 1)

        content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
        # All three texts present in arrival order, with index markers.
        last_pos = -1
        for i, txt in enumerate(texts, start=1):
            self.assertIn(txt, content)
            self.assertIn(f"[{i}]", content)
            pos = content.find(txt)
            self.assertGreater(pos, last_pos, msg=f"text #{i} ({txt!r}) out of order")
            last_pos = pos
        # Coalesced framing marker — agent treats as one combined request.
        self.assertIn("rapid succession", content)

        # Every BufferedMessage row was forwarded → hard-deleted on confirmed
        # forward (PR-3): the coalesced batch delivered all three, so none of
        # the tenant's buffered rows remain.
        self.assertEqual(BufferedMessage.objects.filter(tenant=tenant).count(), 0)

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_voice_buffered_row_is_singleton_among_text(self, mock_post, _mock_send):
        """A voice BufferedMessage row in the middle of a LINE backlog
        must not fold into the surrounding text coalesce. The text rows
        before it coalesce together; the voice row drains as a singleton
        on the next loop iteration; any text rows behind it then form a
        new coalesced batch (or drain singleton if alone)."""
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        mock_post.return_value = _ok_chat_response("ack")

        user = _make_user(line_user_id="U_v_split")
        tenant = _make_tenant(user)
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": [{"message": {"type": "text", "text": "first text"}}]},
            user_text="first text",
        )
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": [{"message": {"type": "text", "text": "second text"}}]},
            user_text="second text",
        )
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": [{"message": {"type": "audio", "duration": 1200}}]},
            user_text="[voice transcript]",
        )
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": [{"message": {"type": "text", "text": "third text"}}]},
            user_text="third text",
        )

        result = deliver_buffered_messages_task(str(tenant.id))
        self.assertEqual(result["delivered"], 4)
        # 3 POSTs: [text1+text2 coalesced] → [voice singleton] → [text3 singleton].
        self.assertEqual(mock_post.call_count, 3)

        first_content = mock_post.call_args_list[0].kwargs["json"]["messages"][0]["content"]
        self.assertIn("first text", first_content)
        self.assertIn("second text", first_content)
        self.assertNotIn("voice transcript", first_content)
        self.assertNotIn("third text", first_content)

        second_content = mock_post.call_args_list[1].kwargs["json"]["messages"][0]["content"]
        self.assertIn("voice transcript", second_content)
        # Singleton voice drain: no coalesce framing.
        self.assertNotIn("rapid succession", second_content)

        third_content = mock_post.call_args_list[2].kwargs["json"]["messages"][0]["content"]
        self.assertIn("third text", third_content)
        # Singleton text drain after the voice break: no coalesce framing.
        self.assertNotIn("rapid succession", third_content)

    @patch("apps.router.line_webhook._send_line_messages", return_value=True)
    @patch("httpx.post")
    def test_singleton_line_row_preserves_pre_coalesce_shape(self, mock_post, _mock_send):
        """One LINE BufferedMessage row → one POST with raw ``user_text``
        as the content (no coalesce framing). This keeps the over-the-wire
        shape identical to the pre-coalesce behaviour so canary verification
        and tenants on older OpenClaw builds don't see a sudden prompt
        format change for the common single-message case."""
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        mock_post.return_value = _ok_chat_response("ack")

        user = _make_user(line_user_id="U_single")
        tenant = _make_tenant(user)
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="just one message",
        )

        result = deliver_buffered_messages_task(str(tenant.id))
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(mock_post.call_count, 1)

        content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertEqual(content, "just one message")
        self.assertNotIn("rapid succession", content)


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    TELEGRAM_BOT_TOKEN="test-bot-token",
)
class DeliverBufferedTelegramDeleteOnForwardTest(TestCase):
    """The buffered Telegram singleton path hard-deletes the row the instant it
    is forwarded to the woken container (docs/encryption-at-rest-directive.md §7,
    Phase 0). Forwarding converges on ``/v1/chat/completions`` +
    ``relay_ai_response_to_telegram`` (same as the live poller drain), so these
    also cover LEGACY raw-payload rows still draining after the envelope change.
    A FAILED forward must keep the row so the retry/apology machinery works."""

    def _make_telegram_user(self) -> User:
        return User.objects.create_user(
            username=f"hib_tg_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=778899,
            preferred_channel="telegram",
        )

    def _ok_resp(self, content="hi back"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": content}}], "usage": {}, "model": "test"}
        resp.raise_for_status = MagicMock()
        return resp

    @patch("apps.router.pending_queue.relay_ai_response_to_telegram", return_value=True)
    @patch("httpx.post")
    def test_forwarded_telegram_row_is_deleted(self, mock_post, mock_relay):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = self._make_telegram_user()
        tenant = _make_tenant(user)
        # Legacy raw-payload row (pre-envelope) — must still drain. chat_id is
        # absent from the raw update, so it falls back to tenant.user.
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.TELEGRAM,
            payload={"update_id": 1, "message": {"text": "hi"}},
            user_text="hi",
        )
        mock_post.return_value = self._ok_resp()

        result = deliver_buffered_messages_task(str(tenant.id))

        self.assertEqual(result["delivered"], 1)
        # Converged forward: /v1/chat/completions with the redacted user_text.
        mock_post.assert_called_once()
        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertTrue(url.endswith("/v1/chat/completions"))
        self.assertEqual(kwargs["json"]["messages"][0]["content"], "hi")
        # Reply relayed to Telegram (rehydrating seam) for the fallback chat_id.
        mock_relay.assert_called_once()
        self.assertEqual(mock_relay.call_args[0][1], 778899)
        # Row hard-deleted on confirmed forward.
        self.assertFalse(BufferedMessage.objects.filter(id=msg.id).exists())

    @patch("apps.router.pending_queue.relay_ai_response_to_telegram")
    @patch("httpx.post")
    def test_failed_telegram_forward_keeps_row(self, mock_post, mock_relay):
        import httpx

        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = self._make_telegram_user()
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.TELEGRAM,
            payload={"update_id": 2, "message": {"text": "hi"}},
            user_text="hi",
        )
        # 4xx → raise_for_status raises immediately (no transient retry/sleep).
        bad = MagicMock()
        bad.status_code = 404
        bad.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())
        mock_post.return_value = bad

        with self.assertRaises(RuntimeError):
            deliver_buffered_messages_task(str(tenant.id))

        mock_relay.assert_not_called()
        # Forward failed → row preserved (not deleted), attempt advanced.
        msg.refresh_from_db()
        self.assertFalse(msg.delivered)
        self.assertEqual(msg.delivery_attempts, 1)


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class CleanupDeliveredBuffersTaskTest(TestCase):
    """The residual sweeper deletes delivered rows older than 7 days and
    undelivered rows older than 30 days (dead-tenant raw webhooks — the
    highest-sensitivity rows in the system), sparing anything more recent."""

    def _make_buffer(self, tenant, *, delivered: bool, age_days: int) -> BufferedMessage:
        from datetime import timedelta

        from django.utils import timezone

        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"events": []},
            user_text="x",
            delivered=delivered,
            delivery_status=(BufferedMessage.Status.DELIVERED if delivered else BufferedMessage.Status.PENDING),
        )
        BufferedMessage.objects.filter(id=msg.id).update(created_at=timezone.now() - timedelta(days=age_days))
        return msg

    def test_deletes_old_delivered_and_undelivered_spares_recent(self):
        from apps.orchestrator.hibernation import cleanup_delivered_buffers_task

        user = _make_user(line_user_id="U_cleanup")
        tenant = _make_tenant(user)

        old_delivered = self._make_buffer(tenant, delivered=True, age_days=10)
        recent_delivered = self._make_buffer(tenant, delivered=True, age_days=2)
        old_undelivered = self._make_buffer(tenant, delivered=False, age_days=40)
        recent_undelivered = self._make_buffer(tenant, delivered=False, age_days=5)

        result = cleanup_delivered_buffers_task()

        self.assertEqual(result["delivered_deleted"], 1)
        self.assertEqual(result["undelivered_deleted"], 1)
        self.assertEqual(result["deleted"], 2)

        remaining = set(BufferedMessage.objects.values_list("id", flat=True))
        self.assertNotIn(old_delivered.id, remaining)
        self.assertNotIn(old_undelivered.id, remaining)
        self.assertIn(recent_delivered.id, remaining)
        self.assertIn(recent_undelivered.id, remaining)


@override_settings(NBHD_INTERNAL_API_KEY="test-key", TELEGRAM_BOT_TOKEN="test-bot-token")
class DeliverBufferedTelegramMinimalEnvelopeTest(TestCase):
    """The converged Telegram drain reconstructs its forward from the NEW
    minimal envelope (schema min-v1): the redacted user_text goes to
    /v1/chat/completions and the reply is relayed to the envelope's chat_id
    (docs/encryption-at-rest-directive.md §7, Phase 0)."""

    def _make_telegram_user(self, chat_id: int) -> User:
        return User.objects.create_user(
            username=f"hib_tgm_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=chat_id,
            preferred_channel="telegram",
        )

    def _ok_resp(self, content="ok"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": content}}], "usage": {}, "model": "t"}
        resp.raise_for_status = MagicMock()
        return resp

    @patch("apps.router.pending_queue.relay_ai_response_to_telegram", return_value=True)
    @patch("httpx.post")
    def test_new_envelope_row_drains_and_relays_to_envelope_chat_id(self, mock_post, mock_relay):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = self._make_telegram_user(chat_id=111222)  # tenant's stored chat id
        tenant = _make_tenant(user)
        msg = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.TELEGRAM,
            payload={
                "schema": "min-v1",
                "channel": "telegram",
                "is_voice": False,
                "is_image": False,
                "chat_id": 999888,  # the message's own chat id — must win
            },
            user_text="log my run",
        )
        mock_post.return_value = self._ok_resp()

        result = deliver_buffered_messages_task(str(tenant.id))

        self.assertEqual(result["delivered"], 1)
        self.assertEqual(mock_post.call_args[1]["json"]["messages"][0]["content"], "log my run")
        # Relayed to the MESSAGE's chat_id from the envelope, not the tenant default.
        self.assertEqual(mock_relay.call_args[0][1], 999888)
        self.assertFalse(BufferedMessage.objects.filter(id=msg.id).exists())

    @patch("apps.router.pending_queue.relay_ai_response_to_telegram", return_value=True)
    @patch("httpx.post")
    def test_media_envelope_injects_resend_marker(self, mock_post, mock_relay):
        from apps.orchestrator.hibernation import deliver_buffered_messages_task

        user = self._make_telegram_user(chat_id=222333)
        tenant = _make_tenant(user)
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.TELEGRAM,
            payload={
                "schema": "min-v1",
                "channel": "telegram",
                "is_voice": False,
                "is_image": True,
                "chat_id": 222333,
                "media": {"photo_file_id": "large_xyz"},
            },
            user_text="check my form",
        )
        mock_post.return_value = self._ok_resp()

        deliver_buffered_messages_task(str(tenant.id))

        content = mock_post.call_args[1]["json"]["messages"][0]["content"]
        self.assertIn("check my form", content)  # caption preserved
        self.assertIn("resend", content.lower())  # agent told media is unavailable, not silently dropped


@override_settings(NBHD_INTERNAL_API_KEY="test-key", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class BufferedLineVoiceSingletonTest(TestCase):
    """The minimal envelope's explicit is_voice flag is honored by the drain's
    batch claim — a voice head is a singleton (not coalesced with following
    text), matching the guard's documented intent."""

    def test_new_envelope_voice_head_is_singleton(self):
        from apps.orchestrator.hibernation import _claim_buffered_batch_for_tenant

        user = _make_user(line_user_id="U_voice_single")
        tenant = _make_tenant(user)
        voice = BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"schema": "min-v1", "channel": "line", "is_voice": True},
            user_text='🎤 Voice message: "log a 5k run"',
        )
        BufferedMessage.objects.create(
            tenant=tenant,
            channel=BufferedMessage.Channel.LINE,
            payload={"schema": "min-v1", "channel": "line", "is_voice": False},
            user_text="and note it felt easy",
        )

        batch, info = _claim_buffered_batch_for_tenant(tenant, BufferedMessage.Channel.LINE, 30.0)

        self.assertEqual(info, {})
        self.assertEqual([b.id for b in batch], [voice.id])  # voice head → singleton batch
