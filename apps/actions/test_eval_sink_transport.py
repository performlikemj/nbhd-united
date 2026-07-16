"""Eval-sink isolation for gate-result edits and expiry follow-ups."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.actions.messaging import update_gate_message
from apps.actions.models import ActionStatus, ActionType, PendingAction
from apps.actions.tasks import expire_stale_pending_actions
from apps.tenants.models import Tenant, User


@override_settings(TELEGRAM_BOT_TOKEN="test-token", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class EvalSinkGateResultIsolationTest(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            username="eval_gate_result",
            password="x",
            telegram_chat_id=246810,
            line_user_id="U_eval_gate_result",
        )
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            is_synthetic=True,
            is_eval_sink=True,
        )

    def _action(self, channel: str, *, stale: bool = False) -> PendingAction:
        return PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={"message_id": "abc"},
            display_summary="Trash a message",
            status=ActionStatus.PENDING if stale else ActionStatus.APPROVED,
            platform_message_id="123",
            platform_channel=channel,
            expires_at=timezone.now() - timedelta(seconds=10) if stale else timezone.now() + timedelta(minutes=5),
        )

    def test_med5_post_backfill_result_edits_skip_telegram_and_line(self):
        telegram_action = self._action("telegram")
        line_action = self._action("line")

        with patch("httpx.post") as transport_post:
            update_gate_message(telegram_action)
            update_gate_message(line_action)

        transport_post.assert_not_called()

    def test_med5_expiry_task_rechecks_tenant_before_result_edit(self):
        action = self._action("telegram", stale=True)

        with patch("httpx.post") as transport_post:
            result = expire_stale_pending_actions()

        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.EXPIRED)
        self.assertEqual(result, "Expired 1 actions")
        transport_post.assert_not_called()
