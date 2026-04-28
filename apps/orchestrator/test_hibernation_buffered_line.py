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
