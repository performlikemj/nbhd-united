"""Active provisional bindings must be byte-identical to permanent bindings."""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.pii.authoring import placeholder_redactions, resolve_receipt_values
from apps.pii.egress import redact_known_values
from apps.pii.redactor import (
    confirm_assistant_output,
    redact_known_entities,
    redact_user_message,
    rehydrate_text,
)
from apps.router.chat_views import _serialize_message
from apps.router.models import AppChatMessage, ChatThread
from apps.tenants.models import Tenant, User
from apps.transcripts.capture import capture_transcript_event
from apps.transcripts.models import TranscriptEvent


class ActiveBindingParityTests(SimpleTestCase):
    def setUp(self):
        permanent = {"[PERSON_1]": {"name": "Fakenamealpha"}}
        provisional = {
            "[PERSON_1]": {
                "name": "Fakenamealpha",
                "provisional": True,
                "first_seen_at": "2026-08-28T00:00:00+00:00",
                "last_seen_at": "2026-08-28T00:00:00+00:00",
                "seen_events": ["0" * 32],
                "seen_dates": ["2026-08-28"],
            }
        }
        common = {"pii_denylist": {}, "model_tier": "starter", "user": SimpleNamespace(display_name="Fixture Owner")}
        self.permanent = SimpleNamespace(pii_entity_map=permanent, **common)
        self.provisional = SimpleNamespace(pii_entity_map=provisional, **common)

    def test_inbound_known_masking_is_byte_identical(self):
        text = "Fakenamealpha arrived"
        self.assertEqual(
            redact_known_entities(self.permanent, text),
            redact_known_entities(self.provisional, text),
        )
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            self.assertEqual(
                redact_user_message(text, self.permanent),
                redact_user_message(text, self.provisional),
            )

    def test_egress_and_rehydration_are_byte_identical(self):
        placeholder_text = "Welcome [PERSON_1]"
        self.assertEqual(
            rehydrate_text(placeholder_text, self.permanent.pii_entity_map),
            rehydrate_text(placeholder_text, self.provisional.pii_entity_map),
        )
        self.assertEqual(
            redact_known_values(self.permanent, "Welcome Fakenamealpha", seam="fixture"),
            redact_known_values(self.provisional, "Welcome Fakenamealpha", seam="fixture"),
        )

    def test_receipt_reply_and_transcript_metadata_are_byte_identical(self):
        placeholder_text = "Welcome [PERSON_1]"
        self.assertEqual(
            placeholder_redactions(placeholder_text, self.permanent.pii_entity_map),
            placeholder_redactions(placeholder_text, self.provisional.pii_entity_map),
        )
        # Reply text and transcript delivery both route through this same map-keyed
        # rehydration primitive; lifecycle metadata cannot alter the rendered bytes.
        permanent_reply = rehydrate_text(placeholder_text, self.permanent.pii_entity_map)
        provisional_reply = rehydrate_text(placeholder_text, self.provisional.pii_entity_map)
        self.assertEqual(permanent_reply, provisional_reply)


class RealSurfaceParityTests(TestCase):
    """Lifecycle metadata must not leak into owner-facing surface bytes."""

    _ADDITIVE_REVIEW_FIELDS = {
        "persistence",
        "expires_at",
        "seen_event_count",
        "seen_date_count",
    }

    def setUp(self):
        self.permanent = self._tenant("permanent", {"name": "Fakenamealpha"})
        self.provisional = self._tenant(
            "provisional",
            {
                "name": "Fakenamealpha",
                "provisional": True,
                "first_seen_at": "2026-08-28T00:00:00+00:00",
                "last_seen_at": "2026-08-28T00:00:00+00:00",
                "seen_events": ["0" * 32],
                "seen_dates": ["2026-08-28"],
            },
        )

    @staticmethod
    def _tenant(suffix: str, binding: dict) -> Tenant:
        user = User.objects.create_user(username=f"pii-parity-{suffix}", password="fixture-pass")
        return Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            recall_capture_enabled=True,
            pii_entity_map={"[PERSON_1]": binding},
        )

    @staticmethod
    def _common_review_fields(entry: dict) -> dict:
        return {key: value for key, value in entry.items() if key not in RealSurfaceParityTests._ADDITIVE_REVIEW_FIELDS}

    def test_reply_and_receipt_serialization_are_identical(self):
        rendered = []
        receipts = []
        stored_receipt = {
            "reply_text": {
                "state": "placeholder",
                "writer": "runtime",
                "redactions": [{"placeholder": "[PERSON_1]"}],
            }
        }
        for tenant in (self.permanent, self.provisional):
            thread = ChatThread.objects.create(tenant=tenant, user=tenant.user, is_main=True)
            redactions = placeholder_redactions("Welcome [PERSON_1]", tenant.pii_entity_map)
            message = AppChatMessage.objects.create(
                tenant=tenant,
                user=tenant.user,
                thread=thread,
                client_msg_id=f"fixture-{tenant.pk}",
                user_text="Hello [PERSON_1]",
                reply_text="Welcome [PERSON_1]",
                status=AppChatMessage.Status.READY,
                user_redactions=redactions,
                reply_redactions=redactions,
            )
            serialized = _serialize_message(message, entity_map=tenant.pii_entity_map)
            rendered.append(
                {
                    "reply_text": serialized["reply_text"],
                    "user_redactions": serialized["user_redactions"],
                    "reply_redactions": serialized["reply_redactions"],
                }
            )
            receipts.append(resolve_receipt_values(stored_receipt, tenant.pii_entity_map))

        self.assertEqual(rendered[0], rendered[1])
        self.assertEqual(rendered[0]["reply_text"], "Welcome Fakenamealpha")
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(receipts[0]["reply_text"]["redactions"][0]["value"], "Fakenamealpha")

    def test_transcript_capture_is_identical(self):
        captured = []

        def deterministic_encryption(_tenant, confirmed):
            return SimpleNamespace(
                ciphertext=b"fixture-ciphertext",
                content_hash=hashlib.sha256(confirmed.text.encode()).hexdigest(),
            )

        with patch("apps.transcripts.capture.encrypt_transcript_text", side_effect=deterministic_encryption):
            for index, tenant in enumerate((self.permanent, self.provisional), start=1):
                confirmed = confirm_assistant_output(tenant, "Welcome Fakenamealpha")
                self.assertIsNotNone(confirmed)
                event = capture_transcript_event(
                    tenant=tenant,
                    source_type=TranscriptEvent.SourceType.ASSISTANT_REPLY,
                    source_event_id=f"fixture-reply-{index}",
                    role=TranscriptEvent.Role.ASSISTANT,
                    channel=TranscriptEvent.Channel.IOS,
                    turn_id=uuid.uuid4(),
                    occurred_at=timezone.now(),
                    redaction=confirmed,
                )
                self.assertIsNotNone(event)
                captured.append((confirmed.text, event.content_hash, bytes(event.text_enc)))

        self.assertEqual(captured[0], captured[1])
        self.assertEqual(captured[0][0], "Welcome [PERSON_1]")

    def test_review_api_common_fields_are_identical(self):
        client = APIClient()
        entries = []
        for tenant in (self.permanent, self.provisional):
            client.force_authenticate(user=tenant.user)
            response = client.get("/api/v1/tenants/settings/pii-review-queue/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["total"], 1)
            entries.append(response.data["entries"][0])

        self.assertEqual(
            self._common_review_fields(entries[0]),
            self._common_review_fields(entries[1]),
        )
        self.assertEqual(entries[0]["persistence"], "permanent")
        self.assertEqual(entries[1]["persistence"], "provisional")
        self.assertIsNone(entries[0]["expires_at"])
        self.assertIsNotNone(entries[1]["expires_at"])
