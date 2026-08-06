"""Wave-2 transcript-ledger coverage for router conversation seams."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.crypto import box
from apps.crypto.keys import mint_and_wrap_dek
from apps.orchestrator.azure_client import _MOCK_KEK_REGISTRY
from apps.pii.redactor import DetectedEntity, RedactionOutcome, as_confirmed
from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import (
    DeliveryState,
    Disposition,
    DrainOutcome,
    SendResult,
    _persist_pending_transcript,
    _prepare_pending_transcript,
    drain_pending_messages_for_tenant_task,
)
from apps.router.proactive_context import record_proactive_outbound
from apps.tenants.models import Tenant, User
from apps.transcripts.enc_columns import TRANSCRIPT_EVENT_TEXT
from apps.transcripts.models import (
    TranscriptCaptureQuarantine,
    TranscriptEvent,
    TranscriptIndexOutbox,
)


def _chat_response(text: str = "assistant masked", response_id: str = "resp-wave2") -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.is_success = True
    response.text = ""
    response.json.return_value = {
        "id": response_id,
        "choices": [{"message": {"content": text}}],
        "usage": {},
        "model": "test",
    }
    response.raise_for_status = MagicMock()
    return response


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    TELEGRAM_BOT_TOKEN="test-bot-token",
    LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    NBHD_DISABLE_BACKGROUND_THREADS=True,
)
class RouterTranscriptLedgerTest(TestCase):
    def setUp(self):
        mock_patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        mock_patcher.start()
        self.addCleanup(mock_patcher.stop)
        self.addCleanup(_MOCK_KEK_REGISTRY.clear)

    def _tenant(self, suffix: str, *, enabled: bool = True) -> Tenant:
        user = User.objects.create_user(
            username=f"tx-wave2-{suffix}",
            telegram_chat_id=810000 + User.objects.count(),
            line_user_id=f"U-wave2-{suffix}",
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-wave2.example.com",
            recall_capture_enabled=enabled,
        )
        if enabled:
            mint_and_wrap_dek(tenant)
        return tenant

    def _pending_row(self, tenant, channel, source_id, *, confirmed=True):
        id_key = {
            PendingMessage.Channel.IOS: "client_msg_id",
            PendingMessage.Channel.TELEGRAM: "provider_event_id",
            PendingMessage.Channel.LINE: "webhook_event_id",
        }[channel]
        channel_user_id = {
            PendingMessage.Channel.TELEGRAM: str(tenant.user.telegram_chat_id),
            PendingMessage.Channel.LINE: tenant.user.line_user_id,
        }.get(channel, "")
        if channel == PendingMessage.Channel.IOS:
            thread = ChatThread.objects.create(
                tenant=tenant,
                user=tenant.user,
                is_main=True,
            )
            channel_user_id = str(thread.id)
            AppChatMessage.objects.create(
                tenant=tenant,
                user=tenant.user,
                thread=thread,
                client_msg_id=source_id,
                user_text="masked user",
            )
        return PendingMessage.objects.create(
            tenant=tenant,
            channel=channel,
            channel_user_id=channel_user_id,
            payload={
                "message_text": "masked user",
                "user_param": channel_user_id,
                "user_timezone": "UTC",
                id_key: source_id,
                "redaction": {"confirmed": confirmed, "reason": "redacted"},
            },
            user_text="masked user",
        )

    def _drain(self, tenant, row):
        with (
            patch(
                "apps.cron.gateway_client.get_gateway_token_for_tenant",
                return_value="gateway-token",
            ),
            patch("apps.router.pending_queue.httpx.post", return_value=_chat_response()),
            patch("apps.router.pending_queue._send_telegram_typing_safe"),
            patch(
                "apps.router.pending_queue.relay_ai_response_to_telegram",
                return_value=SendResult(DeliveryState.SENT, 1, 1),
            ),
            patch("apps.router.line_webhook.relay_ai_response_to_line", return_value=True),
            patch("apps.router.pending_queue._capture_conversation_turn"),
            patch("apps.router.pending_queue._store_ios_turn_reply"),
            patch("apps.router.pending_queue._schedule_ios_digest_refresh"),
        ):
            return drain_pending_messages_for_tenant_task(
                str(tenant.id),
                row.channel,
                row.channel_user_id,
            )

    def test_pending_drain_all_channels_captures_before_queue_delete(self):
        cases = (
            (PendingMessage.Channel.IOS, TranscriptEvent.SourceType.IOS_QUEUED),
            (PendingMessage.Channel.TELEGRAM, TranscriptEvent.SourceType.TELEGRAM_POLLER),
            (PendingMessage.Channel.LINE, TranscriptEvent.SourceType.LINE),
        )
        for index, (channel, source_type) in enumerate(cases):
            with self.subTest(channel=channel):
                tenant = self._tenant(f"drain-{channel}")
                source_id = f"source-{index}"
                row = self._pending_row(tenant, channel, source_id)

                result = self._drain(tenant, row)

                self.assertEqual(result["delivered"], 1)
                self.assertFalse(PendingMessage.objects.filter(pk=row.pk).exists())
                events = list(TranscriptEvent.objects.filter(tenant=tenant).order_by("id"))
                self.assertEqual(len(events), 2)
                user_event = next(event for event in events if event.role == "user")
                assistant_event = next(event for event in events if event.role == "assistant")
                self.assertEqual(user_event.source_type, source_type)
                self.assertEqual(user_event.source_event_id, source_id)
                self.assertEqual(assistant_event.source_type, TranscriptEvent.SourceType.ASSISTANT_REPLY)
                self.assertEqual(assistant_event.source_event_id, source_id)
                self.assertEqual(assistant_event.turn_id, user_event.turn_id)
                self.assertEqual(assistant_event.delivery_state, "sent")
                self.assertEqual(assistant_event.delivered_chunks, 1)
                self.assertEqual(assistant_event.total_chunks, 1)
                self.assertEqual(assistant_event.model_response_ref, "resp-wave2")
                table, column = TRANSCRIPT_EVENT_TEXT
                self.assertEqual(box.decrypt(tenant.id, table, column, user_event.text_enc).reveal(), "masked user")
                self.assertEqual(
                    box.decrypt(tenant.id, table, column, assistant_event.text_enc).reveal(),
                    "assistant masked",
                )
                self.assertEqual(TranscriptIndexOutbox.objects.filter(tenant=tenant).count(), 1)

    def test_pending_capture_redelivery_is_idempotent(self):
        tenant = self._tenant("redelivery")
        row = self._pending_row(tenant, PendingMessage.Channel.TELEGRAM, "update-redelivery")
        outcome = DrainOutcome(
            Disposition.DELIVER,
            DeliveryState.SENT,
            gateway_responded=True,
            delivered_chunks=1,
            total_chunks=1,
            assistant_text="assistant masked",
            model_response_ref="resp-redelivery",
        )
        first = _prepare_pending_transcript(tenant, [row], outcome, include_assistant=True)
        second = _prepare_pending_transcript(tenant, [row], outcome, include_assistant=True)
        assert first is not None and second is not None
        self.assertEqual(first.turn_id, second.turn_id)

        with transaction.atomic():
            _persist_pending_transcript(tenant, first)
        with transaction.atomic():
            _persist_pending_transcript(tenant, second)

        self.assertEqual(TranscriptEvent.objects.filter(tenant=tenant).count(), 2)
        self.assertEqual(TranscriptIndexOutbox.objects.filter(tenant=tenant).count(), 1)

    def test_missing_receipt_quarantines_without_blocking_delivery(self):
        tenant = self._tenant("unconfirmed")
        row = self._pending_row(tenant, PendingMessage.Channel.TELEGRAM, "update-unconfirmed")
        row.payload.pop("redaction")
        row.save(update_fields=["payload"])

        result = self._drain(tenant, row)

        self.assertEqual(result["delivered"], 1)
        self.assertFalse(PendingMessage.objects.filter(pk=row.pk).exists())
        quarantine = TranscriptCaptureQuarantine.objects.get(
            tenant=tenant,
            source_type=TranscriptEvent.SourceType.TELEGRAM_POLLER,
        )
        self.assertEqual(quarantine.reason, "pre-receipt-row")
        self.assertTrue(quarantine.permanent_loss)
        self.assertFalse(any("text" in field.name for field in quarantine._meta.fields))

    def test_pending_prepare_error_quarantines_user_and_assistant_before_delete(self):
        tenant = self._tenant("prepare-error")
        row = self._pending_row(tenant, PendingMessage.Channel.TELEGRAM, "prepare-error-update")

        with patch(
            "apps.transcripts.capture.encrypt_transcript_text",
            side_effect=RuntimeError("key broker down"),
        ):
            result = self._drain(tenant, row)

        self.assertEqual(result["delivered"], 1)
        self.assertFalse(PendingMessage.objects.filter(pk=row.pk).exists())
        self.assertFalse(TranscriptEvent.objects.filter(tenant=tenant).exists())
        quarantines = list(TranscriptCaptureQuarantine.objects.filter(tenant=tenant))
        self.assertEqual(len(quarantines), 2)
        self.assertEqual({item.reason for item in quarantines}, {"capture-error"})
        self.assertEqual(
            {item.source_type for item in quarantines},
            {
                TranscriptEvent.SourceType.TELEGRAM_POLLER,
                TranscriptEvent.SourceType.ASSISTANT_REPLY,
            },
        )
        self.assertFalse(any("text" in field.name for field in quarantines[0]._meta.fields))

    def test_pending_persist_error_retries_as_text_free_quarantine(self):
        tenant = self._tenant("persist-error")
        row = self._pending_row(tenant, PendingMessage.Channel.TELEGRAM, "persist-error-update")

        with patch(
            "apps.transcripts.capture.capture_transcript_event",
            side_effect=RuntimeError("ledger write down"),
        ):
            result = self._drain(tenant, row)

        self.assertEqual(result["delivered"], 1)
        self.assertFalse(PendingMessage.objects.filter(pk=row.pk).exists())
        self.assertFalse(TranscriptEvent.objects.filter(tenant=tenant).exists())
        quarantines = list(TranscriptCaptureQuarantine.objects.filter(tenant=tenant))
        self.assertEqual(len(quarantines), 2)
        self.assertEqual({item.reason for item in quarantines}, {"capture-error"})
        self.assertFalse(any("text" in field.name for field in quarantines[0]._meta.fields))

    def test_terminal_ledgers_user_only_and_retry_captures_nothing(self):
        terminal_tenant = self._tenant("terminal")
        terminal_row = self._pending_row(
            terminal_tenant,
            PendingMessage.Channel.TELEGRAM,
            "terminal-update",
        )
        terminal = DrainOutcome(
            Disposition.TERMINAL,
            DeliveryState.FAILED,
            gateway_responded=False,
            reason="budget_exhausted",
        )
        with patch("apps.router.pending_queue._drain_telegram_batch", return_value=terminal):
            result = drain_pending_messages_for_tenant_task(
                str(terminal_tenant.id),
                terminal_row.channel,
                terminal_row.channel_user_id,
            )
        self.assertEqual(result["terminal"], "budget_exhausted")
        self.assertEqual(
            list(TranscriptEvent.objects.filter(tenant=terminal_tenant).values_list("role", flat=True)),
            [TranscriptEvent.Role.USER],
        )

        retry_tenant = self._tenant("retry")
        retry_row = self._pending_row(
            retry_tenant,
            PendingMessage.Channel.TELEGRAM,
            "retry-update",
        )
        retry = DrainOutcome(
            Disposition.RETRY,
            DeliveryState.FAILED,
            gateway_responded=False,
            reason="ceiling_raised",
        )
        with (
            patch("apps.router.pending_queue._drain_telegram_batch", return_value=retry),
            self.assertRaises(RuntimeError),
        ):
            drain_pending_messages_for_tenant_task(
                str(retry_tenant.id),
                retry_row.channel,
                retry_row.channel_user_id,
            )
        self.assertFalse(TranscriptEvent.objects.filter(tenant=retry_tenant).exists())
        self.assertFalse(TranscriptCaptureQuarantine.objects.filter(tenant=retry_tenant).exists())

    def test_telegram_webhook_redacts_or_quarantines_and_flag_off_is_free(self):
        from apps.router.views import _capture_telegram_webhook_transcript

        tenant = self._tenant("webhook")
        result = {
            "id": "webhook-ref",
            "choices": [{"message": {"content": "assistant masked"}}],
        }
        with patch(
            "apps.pii.redactor.redact_user_message_checked",
            return_value=RedactionOutcome("hello [PERSON_1]", True, "redacted"),
        ):
            _capture_telegram_webhook_transcript(
                tenant,
                update_id=991,
                raw_user_text="hello Alice",
                result=result,
            )
        self.assertEqual(TranscriptEvent.objects.filter(tenant=tenant).count(), 2)
        self.assertEqual(
            TranscriptEvent.objects.get(tenant=tenant, role="assistant").delivery_state,
            "",
        )

        failed_tenant = self._tenant("webhook-failed")
        with patch(
            "apps.pii.redactor.redact_user_message_checked",
            return_value=RedactionOutcome("raw Alice", False, "redaction-error"),
        ):
            _capture_telegram_webhook_transcript(
                failed_tenant,
                update_id=992,
                raw_user_text="raw Alice",
                result=result,
            )
        quarantine = TranscriptCaptureQuarantine.objects.get(
            tenant=failed_tenant,
            source_type=TranscriptEvent.SourceType.TELEGRAM_WEBHOOK,
        )
        self.assertTrue(quarantine.permanent_loss)
        self.assertFalse(TranscriptEvent.objects.filter(tenant=failed_tenant, role="user").exists())

        disabled = self._tenant("webhook-disabled", enabled=False)
        with patch("apps.pii.redactor.redact_user_message_checked") as redact:
            _capture_telegram_webhook_transcript(
                disabled,
                update_id=993,
                raw_user_text="raw Alice",
                result=result,
            )
        redact.assert_not_called()
        self.assertFalse(TranscriptEvent.objects.filter(tenant=disabled).exists())

    def test_disabled_flag_skips_pending_and_on_device_capture_work(self):
        tenant = self._tenant("pending-disabled", enabled=False)
        row = self._pending_row(
            tenant,
            PendingMessage.Channel.TELEGRAM,
            "disabled-update",
        )
        with (
            patch("apps.pii.redactor.redaction_receipt") as receipt,
            patch("apps.pii.redactor.confirm_assistant_output") as confirm,
        ):
            result = self._drain(tenant, row)
        self.assertEqual(result["delivered"], 1)
        receipt.assert_not_called()
        confirm.assert_not_called()

        client = APIClient()
        client.force_authenticate(user=tenant.user)
        with (
            patch("apps.pii.redactor.redact_user_message_checked") as redact,
            patch("apps.router.conversation_capture.schedule_user_md_refresh"),
        ):
            response = client.post(
                "/api/v1/chat/turns/",
                {"text": "raw local", "reply_text": "reply", "client_msg_id": "disabled-local"},
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        redact.assert_not_called()
        self.assertFalse(TranscriptEvent.objects.filter(tenant=tenant).exists())
        self.assertFalse(TranscriptCaptureQuarantine.objects.filter(tenant=tenant).exists())

    def test_on_device_preserves_backdate_quarantine_repair_and_failure_is_soft(self):
        tenant = self._tenant("ondevice")
        client = APIClient()
        client.force_authenticate(user=tenant.user)
        occurred_at = timezone.now() - timedelta(days=1)
        with (
            patch(
                "apps.pii.redactor.redact_user_message_checked",
                return_value=RedactionOutcome("masked local", True, "redacted"),
            ),
            patch("apps.router.conversation_capture.schedule_user_md_refresh"),
        ):
            response = client.post(
                "/api/v1/chat/turns/",
                {
                    "text": "raw local",
                    "reply_text": "local reply",
                    "client_msg_id": "local-backdated",
                    "occurred_at": occurred_at.isoformat(),
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.content)
        user_event = TranscriptEvent.objects.get(tenant=tenant, role="user")
        self.assertEqual(user_event.occurred_at, occurred_at)
        self.assertEqual(
            TranscriptEvent.objects.get(tenant=tenant, role="assistant").delivery_state,
            "sent",
        )

        quarantine_tenant = self._tenant("ondevice-quarantine")
        client.force_authenticate(user=quarantine_tenant.user)
        with (
            patch(
                "apps.pii.redactor.redact_user_message_checked",
                return_value=RedactionOutcome("raw local", False, "redaction-error"),
            ),
            patch("apps.router.conversation_capture.schedule_user_md_refresh"),
        ):
            response = client.post(
                "/api/v1/chat/turns/",
                {"text": "raw local", "reply_text": "reply", "client_msg_id": "local-q"},
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        turn = AppChatMessage.objects.get(tenant=quarantine_tenant, client_msg_id="local-q")
        quarantine = TranscriptCaptureQuarantine.objects.get(
            tenant=quarantine_tenant,
            source_type=TranscriptEvent.SourceType.IOS_ONDEVICE,
        )
        self.assertEqual(quarantine.repair_ref, str(turn.id))

        exploding_tenant = self._tenant("ondevice-explodes")
        client.force_authenticate(user=exploding_tenant.user)
        with (
            patch(
                "apps.pii.redactor.redact_user_message_checked",
                return_value=RedactionOutcome("masked", True, "redacted"),
            ),
            patch(
                "apps.transcripts.capture.capture_transcript_event",
                side_effect=RuntimeError("ledger down"),
            ),
            patch("apps.router.conversation_capture.schedule_user_md_refresh"),
        ):
            response = client.post(
                "/api/v1/chat/turns/",
                {"text": "raw", "reply_text": "reply", "client_msg_id": "local-soft"},
                format="json",
            )
        self.assertEqual(response.status_code, 201)

    def test_on_device_reply_uses_full_redaction_for_unmapped_pii(self):
        tenant = self._tenant("ondevice-reply-pii")
        client = APIClient()
        client.force_authenticate(user=tenant.user)
        raw_reply = "I spoke with Mallory Winters"
        self.assertFalse(tenant.pii_entity_map)

        def detect(text, *_args, **_kwargs):
            if "Mallory Winters" not in text:
                return []
            start = text.index("Mallory Winters")
            return [
                DetectedEntity(
                    "PERSON",
                    start,
                    start + len("Mallory Winters"),
                    0.99,
                )
            ]

        with (
            patch("apps.pii.redactor._detect_pii", side_effect=detect) as ner,
            patch("apps.router.conversation_capture.schedule_user_md_refresh"),
        ):
            response = client.post(
                "/api/v1/chat/turns/",
                {
                    "text": "hello",
                    "reply_text": raw_reply,
                    "client_msg_id": "local-reply-pii",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual([args[0] for args, _kwargs in ner.call_args_list], ["hello", raw_reply])
        assistant = TranscriptEvent.objects.get(tenant=tenant, role=TranscriptEvent.Role.ASSISTANT)
        table, column = TRANSCRIPT_EVENT_TEXT
        captured = box.decrypt(tenant.id, table, column, assistant.text_enc).reveal()
        self.assertRegex(captured, r"\[PERSON_\d+\]")
        self.assertNotIn("Mallory Winters", captured)

    def test_on_device_reply_redaction_failure_quarantines_with_repair_ref(self):
        tenant = self._tenant("ondevice-reply-failure")
        client = APIClient()
        client.force_authenticate(user=tenant.user)
        raw_reply = "Mallory Winters called"

        with (
            patch(
                "apps.pii.redactor._detect_pii",
                side_effect=[[], RuntimeError("NER down")],
            ),
            patch("apps.router.conversation_capture.schedule_user_md_refresh"),
        ):
            response = client.post(
                "/api/v1/chat/turns/",
                {
                    "text": "hello",
                    "reply_text": raw_reply,
                    "client_msg_id": "local-reply-failure",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.content)
        turn = AppChatMessage.objects.get(tenant=tenant, client_msg_id="local-reply-failure")
        quarantine = TranscriptCaptureQuarantine.objects.get(
            tenant=tenant,
            source_type=TranscriptEvent.SourceType.ASSISTANT_REPLY,
        )
        self.assertEqual(quarantine.reason, "redaction-error")
        self.assertEqual(quarantine.repair_ref, str(turn.id))
        self.assertFalse(
            TranscriptEvent.objects.filter(
                tenant=tenant,
                role=TranscriptEvent.Role.ASSISTANT,
            ).exists()
        )
        self.assertFalse(any("text" in field.name for field in quarantine._meta.fields))

    def test_proactive_captures_once_and_encrypts_before_atomic(self):
        tenant = self._tenant("proactive")
        confirmed = as_confirmed(RedactionOutcome("proactive masked", True, "redacted"))
        assert confirmed is not None
        original_atomic = transaction.atomic
        encryption_finished = False

        def encrypt(*_args, **_kwargs):
            nonlocal encryption_finished
            encryption_finished = True
            return b"sealed"

        @contextmanager
        def checked_atomic(*args, **kwargs):
            self.assertTrue(encryption_finished)
            with original_atomic(*args, **kwargs):
                yield

        with (
            patch("apps.pii.redactor.confirm_assistant_output", return_value=confirmed),
            patch("apps.crypto.box.encrypt", side_effect=encrypt),
            patch("apps.router.proactive_context.transaction.atomic", side_effect=checked_atomic),
            patch("apps.router.proactive_context._dispatch_ios_push"),
        ):
            outbound = record_proactive_outbound(
                tenant=tenant,
                channel="telegram",
                channel_user_id="provider-id-not-in-thread-key",
                message_text="proactive raw",
            )

        assert outbound is not None
        event = TranscriptEvent.objects.get(tenant=tenant)
        self.assertEqual(event.source_type, TranscriptEvent.SourceType.PROACTIVE)
        self.assertEqual(event.source_event_id, str(outbound.id))
        self.assertEqual(event.thread_key, "telegram")
        self.assertEqual(event.delivery_state, "")
        self.assertEqual(TranscriptIndexOutbox.objects.filter(tenant=tenant).count(), 1)

        disabled = self._tenant("proactive-disabled", enabled=False)
        with (
            patch("apps.pii.redactor.confirm_assistant_output") as confirm,
            patch("apps.router.proactive_context._dispatch_ios_push"),
        ):
            record_proactive_outbound(
                tenant=disabled,
                channel="telegram",
                channel_user_id="123",
                message_text="disabled",
            )
        confirm.assert_not_called()
        self.assertFalse(TranscriptEvent.objects.filter(tenant=disabled).exists())
