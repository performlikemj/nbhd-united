"""Regression coverage for eval-sink transport isolation in router egress."""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.models import AppChatMessage, ChatThread, DeviceToken, PendingMessage, ProactiveOutbound
from apps.tenants.models import Tenant, User

_APNS_SETTINGS = {
    "APNS_AUTH_KEY": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
    "APNS_KEY_ID": "ABC1234567",
    "APNS_TEAM_ID": "TEAM123456",
    "APNS_BUNDLE_ID": "org.hoodunited.nbhd",
}


def _eval_tenant(*, telegram_chat_id: int | None = None, line_user_id: str | None = None) -> Tenant:
    suffix = secrets.token_hex(4)
    user = User.objects.create_user(
        username=f"eval_transport_{suffix}",
        email=f"{suffix}@example.com",
        telegram_chat_id=telegram_chat_id,
        line_user_id=line_user_id,
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="eval-transport.example.com",
        is_synthetic=True,
        is_eval_sink=True,
    )


def _gateway_response(text: str) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {},
        "model": "test",
    }
    response.raise_for_status.return_value = None
    return response


@override_settings(**_APNS_SETTINGS)
class EvalSinkApnsIsolationTest(TestCase):
    def setUp(self):
        self.tenant = _eval_tenant()
        self.user = self.tenant.user
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        DeviceToken.objects.create(
            tenant=self.tenant,
            user=self.user,
            token="a" * 64,
            environment=DeviceToken.Environment.SANDBOX,
        )

    def _message(self, client_msg_id: str) -> AppChatMessage:
        return AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=client_msg_id,
            user_text="question",
            reply_text="answer",
            status=AppChatMessage.Status.READY,
        )

    def test_high1_reply_ready_and_error_never_call_apns(self):
        from apps.router.push_views import notify_app_reply_error, notify_app_reply_ready

        self._message("ready-1")
        self._message("error-1")
        with patch("apps.common.apns.send_push") as send_push:
            notify_app_reply_ready(self.tenant, ["ready-1"], "answer")
            notify_app_reply_error(self.tenant, ["error-1"])

        send_push.assert_not_called()
        self.assertTrue(DeviceToken.objects.filter(tenant=self.tenant).exists())

    def test_apns_funnel_consults_shared_suppression_guard(self):
        """Any future APNs caller using the chokepoint inherits suppression."""
        from apps.router.push_views import _push_to_user_devices

        with (
            patch("apps.router.push_views.suppresses_real_transport", return_value=True) as suppresses,
            patch("apps.common.apns.send_push") as send_push,
        ):
            _push_to_user_devices(
                self.user,
                body="hypothetical new egress",
                thread_id=None,
                collapse_id=None,
                content_available=True,
                extra={"type": "future"},
            )

        suppresses.assert_called_once_with(self.tenant)
        send_push.assert_not_called()

    def test_separate_push_test_token_route_is_also_suppressed(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        with patch("apps.common.apns.send_push") as send_push:
            response = client.post("/api/v1/push/test/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["skipped"], "eval_sink")
        send_push.assert_not_called()


class EvalSinkPendingQueueIsolationTest(TestCase):
    @override_settings(TELEGRAM_BOT_TOKEN="test-token")
    def test_high2_telegram_drain_skips_typing_and_reply_transport(self):
        from apps.router.pending_queue import _drain_telegram_batch

        tenant = _eval_tenant(telegram_chat_id=424242)
        row = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="424242",
            payload={"message_text": "hi", "user_param": "424242", "user_timezone": "UTC"},
            user_text="hi",
        )

        with (
            patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="token"),
            patch("apps.router.pending_queue.httpx.post", return_value=_gateway_response("answer")) as post,
            patch("apps.router.pending_queue.relay_ai_response_to_telegram") as relay,
            patch("apps.router.pending_queue._capture_conversation_turn"),
            patch("apps.router.pending_queue._record_usage_safe"),
        ):
            delivered = _drain_telegram_batch(tenant, [row], timeout=10)

        self.assertTrue(delivered)
        relay.assert_not_called()
        self.assertEqual(post.call_count, 1)
        self.assertIn("/v1/chat/completions", post.call_args.args[0])

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token")
    def test_high2_line_drain_skips_reply_transport(self):
        from apps.router.pending_queue import _drain_line_batch

        tenant = _eval_tenant(line_user_id="U_eval_transport")
        row = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_eval_transport",
            payload={"message_text": "hi", "user_param": "U_eval_transport", "user_timezone": "UTC"},
            user_text="hi",
        )

        with (
            patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="token"),
            patch("apps.router.pending_queue.httpx.post", return_value=_gateway_response("answer")) as post,
            patch("apps.router.line_webhook.relay_ai_response_to_line") as relay,
            patch("apps.router.pending_queue._capture_conversation_turn"),
            patch("apps.router.pending_queue._record_usage_safe"),
        ):
            delivered = _drain_line_batch(tenant, [row], timeout=10)

        self.assertTrue(delivered)
        relay.assert_not_called()
        self.assertEqual(post.call_count, 1)
        self.assertIn("/v1/chat/completions", post.call_args.args[0])


class EvalSinkProactiveIsolationTest(TestCase):
    def test_med4_real_channel_still_records_evidence_but_never_dispatches_apns(self):
        from apps.router.proactive_context import record_proactive_outbound

        tenant = _eval_tenant()
        DeviceToken.objects.create(tenant=tenant, user=tenant.user, token="b" * 64)

        with patch("apps.router.proactive_context._dispatch_ios_push") as dispatch:
            row = record_proactive_outbound(
                tenant=tenant,
                channel=ProactiveOutbound.Channel.APP,
                channel_user_id=str(tenant.user_id),
                message_text="eval evidence",
                job_name="flag-flip-regression",
            )

        self.assertIsNotNone(row)
        self.assertTrue(ProactiveOutbound.objects.filter(id=row.id, tenant=tenant).exists())
        self.assertEqual(row.channel, ProactiveOutbound.Channel.APP)
        dispatch.assert_not_called()
