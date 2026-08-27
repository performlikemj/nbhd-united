from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase, override_settings

from apps.pii.redactor import RedactionOutcome, redaction_receipt
from apps.pii.testsupport import neural_ran
from apps.router.chat_views import enqueue_tenant_turn
from apps.router.line_webhook import LineWebhookView
from apps.router.models import BufferedMessage, ChatThread, PendingMessage
from apps.router.pending_queue import _build_batch_chat_content
from apps.router.poller import TelegramPoller
from apps.router.wake_on_message import handle_hibernated_message
from apps.tenants.models import Tenant, User


def _user(*, suffix: str, telegram_chat_id=None, line_user_id=None) -> User:
    return User.objects.create_user(
        username=f"receipt_{suffix}",
        email=f"receipt_{suffix}@example.com",
        telegram_chat_id=telegram_chat_id,
        line_user_id=line_user_id,
        preferred_channel="line" if line_user_id else "telegram",
    )


def _tenant(user: User, *, hibernated=False) -> Tenant:
    from django.utils import timezone

    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-receipt.example.com",
        hibernated_at=timezone.now() if hibernated else None,
    )


class PendingMessageReceiptTest(TestCase):
    @patch("apps.router.chat_views.enqueue_message_for_tenant")
    @patch("apps.pii.redactor._redact_user_message", side_effect=neural_ran("masked ios"))
    def test_ios_enqueue_writes_confirmed_receipt(self, _redact, enqueue):
        user = _user(suffix="ios")
        tenant = _tenant(user)
        thread = ChatThread.objects.create(tenant=tenant, user=user, is_main=True)

        _turn, created = enqueue_tenant_turn(
            tenant=tenant,
            user=user,
            text="raw ios",
            thread=thread,
            client_msg_id="receipt-ios",
        )

        self.assertTrue(created)
        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["redaction"], {"confirmed": True, "reason": "redacted"})
        self.assertEqual(enqueue.call_args.kwargs["user_text_excerpt"], "masked ios")

    @patch("apps.router.pending_queue.enqueue_message_for_tenant")
    @patch("apps.pii.redactor._redact_user_message", side_effect=RuntimeError("detector down"))
    def test_telegram_poller_exception_receipt_and_provider_id(self, _redact, enqueue):
        user = _user(suffix="poller", telegram_chat_id=4455)
        tenant = _tenant(user)
        poller = TelegramPoller()
        poller._http = MagicMock()

        poller._forward_to_container(
            4455,
            tenant,
            "raw telegram",
            provider_event_id=9988,
        )

        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["provider_event_id"], 9988)
        self.assertEqual(payload["redaction"], {"confirmed": False, "reason": "redaction-error"})
        self.assertEqual(enqueue.call_args.kwargs["user_text_excerpt"], "raw telegram")

    @patch("apps.router.pending_queue.enqueue_message_for_tenant")
    @patch("apps.pii.redactor._redact_user_message", side_effect=neural_ran("masked line"))
    def test_line_enqueue_writes_receipt_and_webhook_event_id(self, _redact, enqueue):
        user = _user(suffix="line", line_user_id="Ureceipt")
        tenant = _tenant(user)

        LineWebhookView()._forward_to_container(
            "Ureceipt",
            tenant,
            "raw line",
            webhook_event_id="line-event-7",
        )

        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["webhook_event_id"], "line-event-7")
        self.assertEqual(payload["redaction"], {"confirmed": True, "reason": "redacted"})
        self.assertEqual(enqueue.call_args.kwargs["user_text_excerpt"], "masked line")


class BufferedMessageReceiptTest(TestCase):
    @patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True)
    @patch(
        "apps.pii.redactor.redact_user_message_checked",
        return_value=RedactionOutcome("masked buffer", True, "redacted"),
    )
    def test_buffer_row_writes_receipt_and_provider_id(self, _redact, _wake):
        user = _user(suffix="buffer", telegram_chat_id=9911)
        tenant = _tenant(user, hibernated=True)

        handle_hibernated_message(
            tenant,
            "telegram",
            {"update_id": 7766, "message": {"chat": {"id": 9911}, "text": "raw buffer"}},
            "raw buffer",
        )

        row = BufferedMessage.objects.get(tenant=tenant)
        self.assertEqual(row.user_text, "masked buffer")
        self.assertEqual(row.payload["provider_event_id"], 7766)
        self.assertEqual(row.payload["redaction"], {"confirmed": True, "reason": "redacted"})


class CoalescedReceiptIntegrityTest(TestCase):
    def test_receipts_remain_per_row_when_batch_content_is_rebuilt(self):
        user = _user(suffix="batch", telegram_chat_id=7711)
        tenant = _tenant(user)
        rows = [
            PendingMessage.objects.create(
                tenant=tenant,
                channel="telegram",
                channel_user_id="7711",
                payload={
                    "message_text": "decorated one",
                    "user_timezone": "UTC",
                    "redaction": {"confirmed": True, "reason": "redacted"},
                },
                user_text="masked one",
            ),
            PendingMessage.objects.create(
                tenant=tenant,
                channel="telegram",
                channel_user_id="7711",
                payload={
                    "message_text": "decorated two",
                    "user_timezone": "UTC",
                    "redaction": {"confirmed": False, "reason": "redaction-error"},
                },
                user_text="raw two",
            ),
        ]

        content, _user_param, _tz = _build_batch_chat_content(rows, "7711", channel="telegram")

        self.assertIn("masked one", content)
        self.assertIn("raw two", content)
        self.assertTrue(redaction_receipt(rows[0].payload).confirmed)
        self.assertFalse(redaction_receipt(rows[1].payload).confirmed)
        self.assertEqual(redaction_receipt(rows[1].payload).reason, "redaction-error")


@override_settings(TELEGRAM_WEBHOOK_SECRET="receipt-secret", ROUTER_RATE_LIMIT_PER_MINUTE=10)
class TelegramWebhookCaptureReceiptTest(TestCase):
    @patch("apps.router.conversation_capture.record_conversation_turn")
    @patch("apps.router.views.forward_to_openclaw", new_callable=AsyncMock)
    def test_live_webhook_marks_raw_capture_unconfirmed(self, forward, record):
        user = _user(suffix="webhook", telegram_chat_id=8822)
        tenant = _tenant(user)
        forward.return_value = {"choices": [{"message": {"content": "ok"}}]}

        response = self.client.post(
            "/api/v1/telegram/webhook/",
            data={
                "update_id": 6655,
                "message": {"chat": {"id": 8822}, "text": "raw webhook"},
            },
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="receipt-secret",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(record.call_args.kwargs["tenant"], tenant)
        self.assertEqual(
            record.call_args.kwargs["source_payload"],
            {
                "provider_event_id": 6655,
                "redaction": {"confirmed": False, "reason": "seam-unredacted"},
            },
        )
