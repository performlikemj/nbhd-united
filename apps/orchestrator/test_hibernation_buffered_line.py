"""Regression: hibernation buffered LINE delivery must reuse the live
webhook formatter so markdown is stripped and Flex bubbles are used."""

from __future__ import annotations

import json
import secrets
from unittest.mock import MagicMock, patch

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
