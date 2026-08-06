"""Regression coverage for delivery truth in the pending-message drain."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import (
    DeliveryState,
    SendResult,
    _send_telegram_html_chunks,
    _send_telegram_markdown,
    drain_pending_messages_for_tenant_task,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _chat_response(text: str = "reply") -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.is_success = True
    response.text = ""
    response.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {},
        "model": "test",
    }
    response.raise_for_status = MagicMock()
    return response


def _credit_response() -> MagicMock:
    response = MagicMock()
    response.status_code = 402
    response.text = ""
    response.is_success = False
    return response


def _transport_response(status: int) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.is_success = 200 <= status < 300
    response.text = "transport failure" if status >= 400 else ""
    return response


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    TELEGRAM_BOT_TOKEN="test-bot-token",
    LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    NBHD_DISABLE_BACKGROUND_THREADS=True,
)
class DeliveryTruthTest(TestCase):
    _chat_id = 780000

    def _tenant(self, *, budget_exempt: bool = False) -> Tenant:
        type(self)._chat_id += 1
        tenant = create_tenant(
            display_name="Delivery Truth",
            telegram_chat_id=type(self)._chat_id,
        )
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_fqdn = "oc-delivery-truth.example.com"
        tenant.is_budget_exempt = budget_exempt
        tenant.save(update_fields=["status", "container_fqdn", "is_budget_exempt"])
        return tenant

    def _row(self, tenant: Tenant, channel: str, channel_user_id: str) -> PendingMessage:
        return PendingMessage.objects.create(
            tenant=tenant,
            channel=channel,
            channel_user_id=channel_user_id,
            payload={
                "message_text": "hello",
                "user_param": channel_user_id,
                "user_timezone": "UTC",
            },
            user_text="hello",
        )

    def _ios_pair(self, tenant: Tenant, client_msg_id: str = "delivery-truth-ios"):
        thread = ChatThread.objects.create(
            tenant=tenant,
            user=tenant.user,
            title="Main",
            is_main=True,
        )
        turn = AppChatMessage.objects.create(
            tenant=tenant,
            user=tenant.user,
            thread=thread,
            client_msg_id=client_msg_id,
            user_text="hello",
        )
        row = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(thread.id),
            payload={
                "message_text": "hello",
                "user_param": f"thread:{thread.id}",
                "user_timezone": "UTC",
                "client_msg_id": client_msg_id,
            },
            user_text="hello",
        )
        return thread, turn, row

    def test_tripped_credit_circuit_terminalizes_each_channel_without_retry(self):
        for channel in (
            PendingMessage.Channel.TELEGRAM,
            PendingMessage.Channel.LINE,
            PendingMessage.Channel.IOS,
        ):
            with self.subTest(channel=channel):
                tenant = self._tenant()
                if channel == PendingMessage.Channel.IOS:
                    thread, turn, row = self._ios_pair(tenant, f"circuit-{channel}")
                    channel_user_id = str(thread.id)
                else:
                    channel_user_id = str(tenant.user.telegram_chat_id) if channel == "telegram" else "U-circuit"
                    row = self._row(tenant, channel, channel_user_id)
                    turn = None

                with (
                    patch(
                        "apps.cron.gateway_client.get_gateway_token_for_tenant",
                        return_value="gateway-token",
                    ),
                    patch("apps.router.pending_queue.httpx.post", return_value=_credit_response()) as container_post,
                    patch("apps.router.pending_queue._send_telegram_typing_safe"),
                    patch("apps.router.views._hibernate_for_quota") as hibernate,
                    patch("apps.router.billing_quota_handlers.send_cost_exhausted_email"),
                    patch("apps.router.pending_queue._send_telegram_markdown") as telegram_notice,
                    patch("apps.router.line_webhook._send_line_text") as line_notice,
                    patch("apps.router.pending_queue._send_apology_for_dropped_pending_message") as apology,
                    patch("apps.router.pending_queue._reschedule_drain") as reschedule,
                ):
                    result = drain_pending_messages_for_tenant_task(
                        str(tenant.id),
                        channel,
                        channel_user_id,
                    )

                row.refresh_from_db()
                self.assertEqual(row.delivery_status, PendingMessage.Status.FAILED)
                self.assertEqual(result["terminal"], "budget_exhausted")
                self.assertEqual(result["failed"], 1)
                self.assertEqual(container_post.call_count, 1)
                self.assertIn("/v1/chat/completions", container_post.call_args.args[0])
                hibernate.assert_called_once_with(tenant)
                reschedule.assert_not_called()
                apology.assert_not_called()
                if channel == PendingMessage.Channel.TELEGRAM:
                    telegram_notice.assert_called_once()
                    line_notice.assert_not_called()
                elif channel == PendingMessage.Channel.LINE:
                    line_notice.assert_called_once()
                    telegram_notice.assert_not_called()
                else:
                    telegram_notice.assert_not_called()
                    line_notice.assert_not_called()
                    turn.refresh_from_db()
                    self.assertEqual(turn.status, AppChatMessage.Status.ERROR)
                    self.assertEqual(turn.error, "budget_exhausted")

    def test_budget_exempt_credit_limit_retries_and_keeps_ios_turn_pending(self):
        tenant = self._tenant(budget_exempt=True)
        thread, turn, row = self._ios_pair(tenant, "exempt-ios")

        with (
            patch(
                "apps.cron.gateway_client.get_gateway_token_for_tenant",
                return_value="gateway-token",
            ),
            patch("apps.router.pending_queue.httpx.post", return_value=_credit_response()) as container_post,
            patch("apps.billing.credits.sync_or_key_limit") as sync_limit,
            patch("apps.router.views._hibernate_for_quota") as hibernate,
            patch("apps.router.pending_queue._reschedule_drain") as reschedule,
            self.assertRaises(RuntimeError),
        ):
            drain_pending_messages_for_tenant_task(
                str(tenant.id),
                PendingMessage.Channel.IOS,
                str(thread.id),
            )

        row.refresh_from_db()
        turn.refresh_from_db()
        self.assertEqual(row.delivery_status, PendingMessage.Status.PENDING)
        self.assertEqual(row.delivery_attempts, 1)
        self.assertEqual(turn.status, AppChatMessage.Status.PENDING)
        sync_limit.assert_called_once_with(tenant)
        hibernate.assert_not_called()
        reschedule.assert_not_called()
        self.assertEqual(container_post.call_count, 1)

    def test_telegram_partial_chunks_are_delivered_with_counts(self):
        tenant = self._tenant()
        chat_id = str(tenant.user.telegram_chat_id)
        row = self._row(tenant, PendingMessage.Channel.TELEGRAM, chat_id)
        send_responses = iter([_transport_response(200), _transport_response(500)])

        def post_route(url, *args, **kwargs):
            if "/v1/chat/completions" in url:
                return _chat_response("two chunks")
            return next(send_responses)

        with (
            patch(
                "apps.cron.gateway_client.get_gateway_token_for_tenant",
                return_value="gateway-token",
            ),
            patch("apps.router.pending_queue.httpx.post", side_effect=post_route),
            patch("apps.router.pending_queue._send_telegram_typing_safe"),
            patch("apps.router.telegram_format.render_telegram_html", return_value=["one", "two"]),
            patch("apps.router.pending_queue.time.sleep"),
        ):
            result = drain_pending_messages_for_tenant_task(
                str(tenant.id),
                PendingMessage.Channel.TELEGRAM,
                chat_id,
            )

        self.assertFalse(PendingMessage.objects.filter(id=row.id).exists())
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["delivery"], DeliveryState.PARTIAL.value)
        self.assertEqual(result["delivered_chunks"], 1)
        self.assertEqual(result["total_chunks"], 2)

    def test_empty_telegram_render_is_failed_and_bool_wrapper_is_false(self):
        with (
            patch("apps.router.pending_queue.blocks_real_transport_for_identifier", return_value=False),
            patch("apps.router.pending_queue._telegram_api_base", return_value="https://telegram.invalid/bot"),
            patch("apps.router.telegram_format.render_telegram_html", return_value=[]),
        ):
            result = _send_telegram_html_chunks(123, "markers")
            wrapped = _send_telegram_markdown(123, "markers")

        self.assertEqual(result.state, DeliveryState.FAILED)
        self.assertEqual(result.detail, "empty_render")
        self.assertIs(wrapped, False)

    def test_line_relay_exception_is_ambiguous_and_delivered(self):
        tenant = self._tenant()
        row = self._row(tenant, PendingMessage.Channel.LINE, "U-ambiguous")

        with (
            patch(
                "apps.cron.gateway_client.get_gateway_token_for_tenant",
                return_value="gateway-token",
            ),
            patch("apps.router.pending_queue.httpx.post", return_value=_chat_response()),
            patch(
                "apps.router.line_webhook.relay_ai_response_to_line",
                side_effect=RuntimeError("connection closed after send"),
            ),
        ):
            result = drain_pending_messages_for_tenant_task(
                str(tenant.id),
                PendingMessage.Channel.LINE,
                "U-ambiguous",
            )

        self.assertEqual(result["delivery"], DeliveryState.AMBIGUOUS.value)
        self.assertEqual(result["delivered"], 1)
        self.assertFalse(PendingMessage.objects.filter(id=row.id).exists())

    def test_line_relay_false_retries_without_marking_delivered(self):
        tenant = self._tenant()
        row = self._row(tenant, PendingMessage.Channel.LINE, "U-failed")

        with (
            patch(
                "apps.cron.gateway_client.get_gateway_token_for_tenant",
                return_value="gateway-token",
            ),
            patch("apps.router.pending_queue.httpx.post", return_value=_chat_response()),
            patch("apps.router.line_webhook.relay_ai_response_to_line", return_value=False),
            self.assertRaises(RuntimeError),
        ):
            drain_pending_messages_for_tenant_task(
                str(tenant.id),
                PendingMessage.Channel.LINE,
                "U-failed",
            )

        row.refresh_from_db()
        self.assertEqual(row.delivery_status, PendingMessage.Status.PENDING)
        self.assertEqual(row.delivery_attempts, 1)

    def test_telegram_and_line_happy_paths_relay_and_delete(self):
        cases = (
            (PendingMessage.Channel.TELEGRAM, "telegram"),
            (PendingMessage.Channel.LINE, "line"),
        )
        for channel, label in cases:
            with self.subTest(channel=channel):
                tenant = self._tenant()
                channel_user_id = str(tenant.user.telegram_chat_id) if channel == "telegram" else "U-happy"
                row = self._row(tenant, channel, channel_user_id)
                telegram_result = SendResult(
                    DeliveryState.SENT,
                    delivered_chunks=1,
                    total_chunks=1,
                )

                with (
                    patch(
                        "apps.cron.gateway_client.get_gateway_token_for_tenant",
                        return_value="gateway-token",
                    ),
                    patch("apps.router.pending_queue.httpx.post", return_value=_chat_response()),
                    patch("apps.router.pending_queue._send_telegram_typing_safe"),
                    patch(
                        "apps.router.pending_queue.relay_ai_response_to_telegram",
                        return_value=telegram_result,
                    ) as telegram_relay,
                    patch("apps.router.line_webhook.relay_ai_response_to_line", return_value=True) as line_relay,
                ):
                    result = drain_pending_messages_for_tenant_task(
                        str(tenant.id),
                        channel,
                        channel_user_id,
                    )

                self.assertEqual(result["delivered"], 1, label)
                self.assertFalse(PendingMessage.objects.filter(id=row.id).exists())
                if channel == PendingMessage.Channel.TELEGRAM:
                    telegram_relay.assert_called_once()
                    line_relay.assert_not_called()
                else:
                    line_relay.assert_called_once()
                    telegram_relay.assert_not_called()

    def test_ios_reply_persists_deletes_queue_row_and_refreshes_digest(self):
        tenant = self._tenant()
        thread, turn, row = self._ios_pair(tenant, "happy-ios")

        with (
            patch(
                "apps.cron.gateway_client.get_gateway_token_for_tenant",
                return_value="gateway-token",
            ),
            patch("apps.router.pending_queue.httpx.post", return_value=_chat_response("stored reply")),
            patch("apps.router.pending_queue._schedule_ios_digest_refresh") as refresh_digest,
        ):
            result = drain_pending_messages_for_tenant_task(
                str(tenant.id),
                PendingMessage.Channel.IOS,
                str(thread.id),
            )

        turn.refresh_from_db()
        self.assertEqual(result["delivered"], 1)
        self.assertFalse(PendingMessage.objects.filter(id=row.id).exists())
        self.assertEqual(turn.status, AppChatMessage.Status.READY)
        self.assertEqual(turn.reply_text, "stored reply")
        refresh_digest.assert_called_once_with(tenant)
