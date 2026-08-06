from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.crypto import box
from apps.crypto.keys import mint_and_wrap_dek
from apps.orchestrator.azure_client import _MOCK_KEK_REGISTRY
from apps.pii.redactor import ConfirmedRedaction, RedactionOutcome, as_confirmed
from apps.tenants.models import Tenant, User

from .alerts import check_quarantine_alerts
from .capture import (
    capture_transcript_event,
    encrypt_transcript_text,
    quarantine_transcript_event,
)
from .enc_columns import TRANSCRIPT_EVENT_TEXT
from .models import TranscriptCaptureQuarantine, TranscriptEvent, TranscriptIndexOutbox


def _tenant(suffix: str, *, enabled: bool = True) -> Tenant:
    user = User.objects.create_user(username=f"transcripts-{suffix}", password="pass1234")
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        recall_capture_enabled=enabled,
    )


def _confirmed(text: str = "hello [PERSON_1]") -> ConfirmedRedaction:
    confirmed = as_confirmed(RedactionOutcome(text, True, "redacted"))
    assert confirmed is not None
    return confirmed


def _capture_kwargs(tenant: Tenant, *, source_event_id: str = "event-1") -> dict:
    return {
        "tenant": tenant,
        "source_type": TranscriptEvent.SourceType.IOS_QUEUED,
        "source_event_id": source_event_id,
        "role": TranscriptEvent.Role.USER,
        "channel": TranscriptEvent.Channel.IOS,
        "turn_id": uuid.uuid4(),
        "occurred_at": timezone.now(),
    }


class CaptureTest(TestCase):
    def setUp(self):
        mock_patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        mock_patcher.start()
        self.addCleanup(mock_patcher.stop)
        self.addCleanup(_MOCK_KEK_REGISTRY.clear)

    def test_confirmed_capture_encrypts_hashes_and_creates_outbox(self):
        tenant = _tenant("happy")
        mint_and_wrap_dek(tenant)
        plaintext = "hello [PERSON_1]"

        event = capture_transcript_event(
            **_capture_kwargs(tenant),
            redaction=_confirmed(plaintext),
        )

        self.assertIsNotNone(event)
        event.refresh_from_db()
        table, column = TRANSCRIPT_EVENT_TEXT
        decrypted = box.decrypt(tenant.id, table, column, event.text_enc)
        self.assertEqual(decrypted.reveal(), plaintext)
        self.assertEqual(event.content_hash, hashlib.sha256(plaintext.encode()).hexdigest())
        self.assertEqual(TranscriptIndexOutbox.objects.filter(tenant=tenant, turn_id=event.turn_id).count(), 1)
        self.assertNotIn("text", {field.name for field in event._meta.fields})

    def test_unconfirmed_outcome_is_type_incapable_of_capture(self):
        tenant = _tenant("unconfirmed")

        with self.assertRaises(TypeError):
            capture_transcript_event(
                **_capture_kwargs(tenant),
                redaction=RedactionOutcome("raw Alice", False, "redaction-error"),
            )

        self.assertFalse(TranscriptEvent.objects.exists())

    def test_forged_confirmation_is_refused(self):
        tenant = _tenant("forged")
        forged = ConfirmedRedaction(text="raw Alice", reason="redacted", _provenance=object())

        with self.assertRaises(ValueError):
            capture_transcript_event(
                **_capture_kwargs(tenant),
                redaction=forged,
            )

        self.assertFalse(TranscriptEvent.objects.exists())

    def test_quarantine_stores_no_text_and_marks_provider_loss_permanent(self):
        tenant = _tenant("quarantine")
        outcome = RedactionOutcome("raw Alice must never persist", False, "redaction-error")

        with patch("apps.transcripts.alerts.check_quarantine_alerts"), self.captureOnCommitCallbacks(execute=True):
            row = quarantine_transcript_event(
                tenant=tenant,
                source_type=TranscriptEvent.SourceType.TELEGRAM_WEBHOOK,
                source_event_id="update-1",
                channel=TranscriptEvent.Channel.TELEGRAM,
                outcome=outcome,
            )

        self.assertTrue(row.permanent_loss)
        self.assertEqual(row.reason, "redaction-error")
        self.assertFalse(any("text" in field.name for field in row._meta.fields))
        self.assertNotIn("raw Alice", repr(row.__dict__))

    def test_capture_and_outbox_are_idempotent(self):
        tenant = _tenant("idempotent")
        mint_and_wrap_dek(tenant)
        kwargs = _capture_kwargs(tenant)
        confirmed = _confirmed()

        first = capture_transcript_event(**kwargs, redaction=confirmed)
        second = capture_transcript_event(
            **(kwargs | {"turn_id": uuid.uuid4()}),
            redaction=confirmed,
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(TranscriptEvent.objects.count(), 1)
        self.assertEqual(TranscriptIndexOutbox.objects.count(), 1)

    def test_disabled_flag_noops_without_encryption_or_writes(self):
        tenant = _tenant("disabled", enabled=False)

        with patch("apps.crypto.box.encrypt") as encrypt_mock:
            event = capture_transcript_event(
                **_capture_kwargs(tenant),
                redaction=_confirmed(),
            )
            quarantine = quarantine_transcript_event(
                tenant=tenant,
                source_type=TranscriptEvent.SourceType.LINE,
                source_event_id="line-1",
                channel=TranscriptEvent.Channel.LINE,
                outcome=RedactionOutcome("raw", False, "redaction-error"),
            )

        self.assertIsNone(event)
        self.assertIsNone(quarantine)
        encrypt_mock.assert_not_called()
        self.assertFalse(TranscriptEvent.objects.exists())
        self.assertFalse(TranscriptCaptureQuarantine.objects.exists())

    def test_pre_encrypted_path_does_not_encrypt_inside_open_transaction(self):
        tenant = _tenant("pre-encrypted")
        mint_and_wrap_dek(tenant)
        prepared = encrypt_transcript_text(tenant, _confirmed())

        with patch("apps.crypto.box.encrypt") as encrypt_mock, transaction.atomic():
            event = capture_transcript_event(
                **_capture_kwargs(tenant),
                redaction=prepared,
            )

        self.assertIsNotNone(event)
        encrypt_mock.assert_not_called()


class RecallCaptureCommandTest(TestCase):
    def test_birthday_is_preserved_across_reenable_in_pre_purge_lifecycle(self):
        # P1 has no 30-day purge; the lifecycle phase will pair that purge with
        # C8's required post-purge birthday re-stamp.
        tenant = _tenant("birthday", enabled=False)
        out = StringIO()

        call_command("set_recall_capture", str(tenant.id), "--on", stdout=out)
        tenant.refresh_from_db()
        first_birthday = tenant.recall_capture_birthday
        self.assertTrue(tenant.recall_capture_enabled)
        self.assertIsNotNone(first_birthday)

        call_command("set_recall_capture", str(tenant.id), "--off", stdout=out)
        tenant.refresh_from_db()
        self.assertFalse(tenant.recall_capture_enabled)
        self.assertEqual(tenant.recall_capture_birthday, first_birthday)

        call_command("set_recall_capture", str(tenant.id), "--on", stdout=out)
        tenant.refresh_from_db()
        self.assertTrue(tenant.recall_capture_enabled)
        self.assertEqual(tenant.recall_capture_birthday, first_birthday)
        self.assertIn("recall_capture=on", out.getvalue())


@override_settings(PLATFORM_OWNER_EMAIL="owner@nbhd.test")
class QuarantineAlertTest(TestCase):
    def test_rate_above_one_percent_over_twenty_attempts_is_cooldown_deduped(self):
        tenant = _tenant("rate-alert")
        now = timezone.now()
        TranscriptEvent.objects.bulk_create(
            [
                TranscriptEvent(
                    tenant=tenant,
                    turn_id=uuid.uuid4(),
                    role=TranscriptEvent.Role.USER,
                    source_type=TranscriptEvent.SourceType.IOS_QUEUED,
                    source_event_id=f"captured-{index}",
                    channel=TranscriptEvent.Channel.IOS,
                    occurred_at=now - timedelta(minutes=index),
                    text_enc=b"",
                    content_hash="0" * 64,
                )
                for index in range(19)
            ]
        )
        TranscriptCaptureQuarantine.objects.create(
            tenant=tenant,
            source_type=TranscriptEvent.SourceType.IOS_QUEUED,
            source_event_id="quarantine-1",
            channel=TranscriptEvent.Channel.IOS,
            reason="redaction-error",
        )

        with (
            patch("apps.transcripts.alerts.should_send", side_effect=[now, None]),
            patch("apps.transcripts.alerts.send_mail", return_value=1) as send_mail,
            patch("apps.transcripts.alerts.record_sent"),
            patch("apps.transcripts.alerts.record_suppressed") as suppressed,
        ):
            check_quarantine_alerts(tenant)
            check_quarantine_alerts(tenant)

        send_mail.assert_called_once()
        suppressed.assert_called_once()
        self.assertIn("above 1%", send_mail.call_args.kwargs["subject"])
        self.assertIn("Quarantines: 1", send_mail.call_args.kwargs["message"])

    def test_any_permanent_loss_fires_loud_alert(self):
        tenant = _tenant("permanent-alert")
        TranscriptCaptureQuarantine.objects.create(
            tenant=tenant,
            source_type=TranscriptEvent.SourceType.LINE,
            source_event_id="line-event-1",
            channel=TranscriptEvent.Channel.LINE,
            reason="redaction-error",
            permanent_loss=True,
        )

        with (
            patch("apps.transcripts.alerts.should_send", return_value=timezone.now()),
            patch("apps.transcripts.alerts.send_mail", return_value=1) as send_mail,
            patch("apps.transcripts.alerts.record_sent"),
        ):
            check_quarantine_alerts(tenant)

        send_mail.assert_called_once()
        self.assertIn("Permanent capture loss", send_mail.call_args.kwargs["subject"])
        self.assertIn("Source type: line", send_mail.call_args.kwargs["message"])
