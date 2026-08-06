"""Wave-2 transcript-ledger coverage for hibernation buffered delivery."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.crypto import box
from apps.crypto.keys import mint_and_wrap_dek
from apps.orchestrator.azure_client import _MOCK_KEK_REGISTRY
from apps.orchestrator.hibernation import deliver_buffered_messages_task
from apps.router.models import BufferedMessage
from apps.router.pending_queue import DeliveryState, SendResult
from apps.tenants.models import Tenant, User
from apps.transcripts.enc_columns import TRANSCRIPT_EVENT_TEXT
from apps.transcripts.models import TranscriptCaptureQuarantine, TranscriptEvent


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    TELEGRAM_BOT_TOKEN="test-bot-token",
)
class BufferedTranscriptLedgerTest(TestCase):
    def setUp(self):
        mock_patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        mock_patcher.start()
        self.addCleanup(mock_patcher.stop)
        self.addCleanup(_MOCK_KEK_REGISTRY.clear)

    def _tenant(self, suffix: str, *, enabled: bool = True) -> Tenant:
        user = User.objects.create_user(
            username=f"buffered-tx-{suffix}",
            telegram_chat_id=920000 + User.objects.count(),
            line_user_id=f"U-buffered-{suffix}",
            preferred_channel="telegram",
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-buffered-wave2.example.com",
            recall_capture_enabled=enabled,
        )
        if enabled:
            mint_and_wrap_dek(tenant)
        return tenant

    def _row(self, tenant, channel, *, receipt=True):
        payload = {
            "schema": 1,
            "telegram_chat_id": tenant.user.telegram_chat_id,
        }
        if receipt:
            payload["redaction"] = {"confirmed": True, "reason": "redacted"}
        return BufferedMessage.objects.create(
            tenant=tenant,
            channel=channel,
            payload=payload,
            user_text="buffered masked",
        )

    def _result(self):
        return {
            "id": "buffered-response-ref",
            "choices": [{"message": {"content": "buffered assistant"}}],
        }

    def test_telegram_buffered_user_and_assistant_are_captured_before_delete(self):
        tenant = self._tenant("telegram")
        row = self._row(tenant, BufferedMessage.Channel.TELEGRAM)
        with (
            patch(
                "apps.orchestrator.hibernation._post_chat_completion_with_backoff",
                return_value=self._result(),
            ),
            patch(
                "apps.cron.gateway_client.get_gateway_token_for_tenant",
                return_value="gateway-token",
            ),
            patch(
                "apps.router.pending_queue.relay_ai_response_to_telegram",
                return_value=SendResult(DeliveryState.SENT, 1, 1),
            ),
        ):
            result = deliver_buffered_messages_task(str(tenant.id))

        self.assertEqual(result["delivered"], 1)
        self.assertFalse(BufferedMessage.objects.filter(pk=row.pk).exists())
        events = list(TranscriptEvent.objects.filter(tenant=tenant).order_by("id"))
        self.assertEqual(len(events), 2)
        self.assertEqual({event.source_event_id for event in events}, {str(row.id)})
        self.assertEqual({event.turn_id for event in events}, {events[0].turn_id})
        assistant = next(event for event in events if event.role == "assistant")
        self.assertEqual(assistant.delivery_state, "sent")
        self.assertEqual(assistant.model_response_ref, "buffered-response-ref")
        table, column = TRANSCRIPT_EVENT_TEXT
        user_event = next(event for event in events if event.role == "user")
        self.assertEqual(
            box.decrypt(tenant.id, table, column, user_event.text_enc).reveal(),
            "buffered masked",
        )

    def test_line_failed_relay_truth_and_unconfirmed_user_survive_delete(self):
        tenant = self._tenant("line-failed")
        row = self._row(tenant, BufferedMessage.Channel.LINE, receipt=False)
        with (
            patch(
                "apps.orchestrator.hibernation._post_chat_completion_with_backoff",
                return_value=self._result(),
            ),
            patch(
                "apps.cron.gateway_client.get_gateway_token_for_tenant",
                return_value="gateway-token",
            ),
            patch("apps.router.line_webhook.relay_ai_response_to_line", return_value=False),
        ):
            result = deliver_buffered_messages_task(str(tenant.id))

        # Residual retained intentionally: the buffer still reports delivered
        # and is deleted even though relay truth is failed.
        self.assertEqual(result["delivered"], 1)
        self.assertFalse(BufferedMessage.objects.filter(pk=row.pk).exists())
        assistant = TranscriptEvent.objects.get(tenant=tenant, role="assistant")
        self.assertEqual(assistant.delivery_state, "failed")
        quarantine = TranscriptCaptureQuarantine.objects.get(tenant=tenant)
        self.assertEqual(quarantine.source_type, TranscriptEvent.SourceType.BUFFERED)
        self.assertEqual(quarantine.source_event_id, str(row.id))
        self.assertEqual(quarantine.reason, "pre-receipt-row")
        self.assertTrue(quarantine.permanent_loss)

    def test_disabled_flag_skips_receipts_confirmation_and_ledger(self):
        tenant = self._tenant("disabled", enabled=False)
        self._row(tenant, BufferedMessage.Channel.TELEGRAM)
        with (
            patch(
                "apps.orchestrator.hibernation._post_chat_completion_with_backoff",
                return_value=self._result(),
            ),
            patch(
                "apps.cron.gateway_client.get_gateway_token_for_tenant",
                return_value="gateway-token",
            ),
            patch(
                "apps.router.pending_queue.relay_ai_response_to_telegram",
                return_value=SendResult(DeliveryState.SENT, 1, 1),
            ),
            patch("apps.pii.redactor.redaction_receipt") as receipt,
            patch("apps.pii.redactor.confirm_assistant_output") as confirm,
        ):
            deliver_buffered_messages_task(str(tenant.id))

        receipt.assert_not_called()
        confirm.assert_not_called()
        self.assertFalse(TranscriptEvent.objects.filter(tenant=tenant).exists())
        self.assertFalse(TranscriptCaptureQuarantine.objects.filter(tenant=tenant).exists())
