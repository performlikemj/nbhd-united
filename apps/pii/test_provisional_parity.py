"""Active provisional bindings must be byte-identical to permanent bindings."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.pii.authoring import placeholder_redactions
from apps.pii.egress import redact_known_values
from apps.pii.redactor import redact_known_entities, redact_user_message, rehydrate_text


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
