"""Regression coverage for eval-sink transport isolation in router egress."""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
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


def _real_tenant(*, telegram_chat_id: int | None = None, line_user_id: str | None = None) -> Tenant:
    suffix = secrets.token_hex(4)
    user = User.objects.create_user(
        username=f"real_transport_{suffix}",
        email=f"{suffix}@example.com",
        telegram_chat_id=telegram_chat_id,
        line_user_id=line_user_id,
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="real-transport.example.com",
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


class EvalSinkPredicateIsolationTest(SimpleTestCase):
    def test_only_literal_true_suppresses_real_transport(self):
        from apps.common.eval_sink import suppresses_real_transport

        self.assertTrue(suppresses_real_transport(MagicMock(is_eval_sink=True)))
        self.assertFalse(suppresses_real_transport(MagicMock(is_eval_sink=False)))
        self.assertFalse(suppresses_real_transport(MagicMock()))


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


@override_settings(TELEGRAM_BOT_TOKEN="test-token", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class EvalSinkPrimitiveIsolationTest(TestCase):
    def test_poller_send_message_blocks_eval_but_sends_real_and_unknown_targets(self):
        from apps.router.poller import TelegramPoller

        eval_tenant = _eval_tenant(telegram_chat_id=910001)
        real_tenant = _real_tenant(telegram_chat_id=910002)
        poller = TelegramPoller()
        poller._http = MagicMock()
        poller._http.post.return_value = MagicMock(is_success=True)

        poller._send_message(eval_tenant.user.telegram_chat_id, "blocked")
        poller._http.post.assert_not_called()

        poller._send_message(real_tenant.user.telegram_chat_id, "sent")
        poller._send_message(919999, "ops target")

        self.assertEqual(poller._http.post.call_count, 2)
        sent_ids = [call.kwargs["json"]["chat_id"] for call in poller._http.post.call_args_list]
        self.assertEqual(sent_ids, [910002, 919999])

    @patch("apps.router.line_webhook.httpx.post")
    def test_line_push_blocks_eval_but_sends_real_and_unknown_targets(self, mock_post):
        from apps.router.line_webhook import _send_line_push

        eval_tenant = _eval_tenant(line_user_id="U_eval_primitive")
        real_tenant = _real_tenant(line_user_id="U_real_primitive")
        response = MagicMock(is_success=True)
        response.json.return_value = {}
        mock_post.return_value = response
        messages = [{"type": "text", "text": "hello"}]

        self.assertFalse(_send_line_push(eval_tenant.user.line_user_id, messages))
        self.assertTrue(_send_line_push(real_tenant.user.line_user_id, messages))
        self.assertTrue(_send_line_push("U_ops_unowned", messages))

        self.assertEqual(mock_post.call_count, 2)
        sent_ids = [call.kwargs["json"]["to"] for call in mock_post.call_args_list]
        self.assertEqual(sent_ids, ["U_real_primitive", "U_ops_unowned"])

    @patch("apps.router.line_webhook.httpx.post")
    def test_line_reply_path_is_blocked_before_reply_api_for_eval_owner(self, mock_post):
        from apps.router.line_webhook import _send_line_messages

        tenant = _eval_tenant(line_user_id="U_eval_reply")

        self.assertFalse(
            _send_line_messages(
                tenant.user.line_user_id,
                [{"type": "text", "text": "blocked"}],
                reply_token="reply-token",
                tenant=tenant,
            )
        )
        mock_post.assert_not_called()

    @patch("apps.router.pending_queue.httpx.post")
    def test_raw_telegram_relay_blocks_eval_and_sends_real_tenant(self, mock_post):
        from apps.router.pending_queue import relay_ai_response_to_telegram

        eval_tenant = _eval_tenant(telegram_chat_id=920001)
        real_tenant = _real_tenant(telegram_chat_id=920002)
        mock_post.return_value = MagicMock(is_success=True, status_code=200)

        self.assertFalse(relay_ai_response_to_telegram(eval_tenant, 920001, "blocked"))
        mock_post.assert_not_called()

        self.assertTrue(relay_ai_response_to_telegram(real_tenant, 920002, "sent"))
        self.assertTrue(any("sendMessage" in call.args[0] for call in mock_post.call_args_list))


@override_settings(
    TELEGRAM_BOT_TOKEN="test-token",
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
    FRONTEND_URL="https://app.example.com",
)
class EvalSinkExceptionalReplyIsolationTest(TestCase):
    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.router.billing_quota_handlers.send_cost_exhausted_email")
    @patch("apps.router.views._hibernate_for_quota")
    def test_credit_limit_blocks_eval_transport_but_real_tenant_still_sends(
        self,
        _hibernate,
        _email,
        mock_post,
    ):
        from apps.router.pending_queue import _handle_openrouter_credit_limit

        eval_tenant = _eval_tenant(telegram_chat_id=930001)
        real_tenant = _real_tenant(telegram_chat_id=930002)
        mock_post.return_value = MagicMock(is_success=True, status_code=200)

        _handle_openrouter_credit_limit(eval_tenant, channel="telegram", channel_user_id="930001")
        mock_post.assert_not_called()

        _handle_openrouter_credit_limit(real_tenant, channel="telegram", channel_user_id="930002")
        self.assertTrue(any("sendMessage" in call.args[0] for call in mock_post.call_args_list))

    @patch("httpx.post")
    def test_stale_and_dropped_apologies_block_eval_transports(self, transport_post):
        from apps.router.pending_queue import (
            _send_apology_for_dropped_pending_message,
            _send_apology_for_stale_pending_message,
        )

        tenant = _eval_tenant(telegram_chat_id=940001, line_user_id="U_eval_apology")
        stale = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="940001",
            payload={},
            user_text="old question",
        )
        dropped = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_eval_apology",
            payload={},
            user_text="failed question",
        )

        _send_apology_for_stale_pending_message(tenant, stale, 900)
        _send_apology_for_dropped_pending_message(tenant, dropped)

        transport_post.assert_not_called()

    @patch("httpx.post")
    def test_stale_and_dropped_apologies_still_send_for_real_tenant(self, transport_post):
        from apps.router.pending_queue import (
            _send_apology_for_dropped_pending_message,
            _send_apology_for_stale_pending_message,
        )

        tenant = _real_tenant(telegram_chat_id=940002, line_user_id="U_real_apology")
        stale = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.TELEGRAM,
            channel_user_id="940002",
            payload={},
            user_text="old question",
        )
        dropped = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_real_apology",
            payload={},
            user_text="failed question",
        )
        response = MagicMock(is_success=True, status_code=200)
        response.json.return_value = {}
        transport_post.return_value = response

        _send_apology_for_stale_pending_message(tenant, stale, 900)
        _send_apology_for_dropped_pending_message(tenant, dropped)

        self.assertTrue(any("sendMessage" in call.args[0] for call in transport_post.call_args_list))
        self.assertTrue(any("message/push" in call.args[0] for call in transport_post.call_args_list))

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
