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

        msg.refresh_from_db()
        # Transient retry must NOT count against the per-message attempt cap.
        self.assertEqual(msg.delivery_attempts, 0)
        self.assertTrue(msg.delivered)
        self.assertEqual(msg.delivery_status, BufferedMessage.Status.DELIVERED)

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
        fresh.refresh_from_db()
        self.assertTrue(fresh.delivered)
        self.assertEqual(fresh.delivery_status, BufferedMessage.Status.DELIVERED)


class ApologyHelperTest(TestCase):
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

        msg.refresh_from_db()
        self.assertTrue(msg.delivered)
        # Lease cleared on success.
        self.assertIsNone(msg.delivery_in_flight_until)

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
        msg.refresh_from_db()
        self.assertTrue(msg.delivered)
        self.assertIsNone(msg.delivery_in_flight_until)

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

        with patch(
            "apps.orchestrator.hibernation._post_chat_completion_with_backoff",
            side_effect=RuntimeError("boom"),
        ), self.assertRaises(RuntimeError):
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
