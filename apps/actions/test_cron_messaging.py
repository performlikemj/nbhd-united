from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from apps.actions.messaging import (
    _edit_telegram_message,
    _review_window_text,
    _send_line_confirmation,
    _send_telegram_confirmation,
)
from apps.actions.models import ActionStatus, ActionType, PendingAction
from apps.tenants.services import create_tenant


@override_settings(TELEGRAM_BOT_TOKEN="telegram-test", LINE_CHANNEL_ACCESS_TOKEN="line-test")
class CronGateMessagingTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Cron Messaging", telegram_chat_id=99101)
        self.tenant.user.line_user_id = "U" + "1" * 32
        self.tenant.user.save(update_fields=["line_user_id"])
        self.action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CRON_CREATE,
            action_payload={},
            display_summary="Create scheduled task Water plants",
            platform_message_id="123",
        )

    @patch("httpx.post")
    def test_telegram_confirmation_uses_reversible_cron_copy_and_72_hours(self, post):
        post.return_value = Mock(
            status_code=200,
            json=lambda: {"result": {"message_id": 123}},
        )

        result = _send_telegram_confirmation(self.tenant, self.action)

        self.assertTrue(result.accepted)
        text = post.call_args.kwargs["json"]["text"]
        self.assertIn("disable this scheduled task later", text)
        self.assertIn("Review within 72 hours", text)
        self.assertNotIn("cannot be undone", text)
        self.assertEqual(_review_window_text(self.action), "Review within 72 hours")

    @patch("httpx.post")
    def test_line_confirmation_uses_reversible_cron_copy(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"sentMessages": [{"id": "line-1"}]})

        _send_line_confirmation(self.tenant, self.action)

        contents = post.call_args.kwargs["json"]["messages"][0]["contents"]["body"]["contents"]
        rendered = " ".join(str(item.get("text", "")) for item in contents)
        self.assertIn("disable this scheduled task later", rendered)
        self.assertNotIn("cannot be undone", rendered)

    @patch("httpx.post")
    def test_telegram_result_renders_execution_outcome_after_approval(self, post):
        self.action.status = ActionStatus.APPROVED
        self.action.resolution_code = "dispatch_failed"
        self.action.save(update_fields=["status", "resolution_code"])

        _edit_telegram_message(self.tenant, self.action)

        text = post.call_args.kwargs["json"]["text"]
        self.assertIn("CREATION FAILED", text)
        self.assertIn("dispatch_failed", text)
