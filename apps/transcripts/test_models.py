from django.db import models
from django.test import SimpleTestCase

from .models import TranscriptCaptureQuarantine, TranscriptEvent


class TranscriptModelShapeTests(SimpleTestCase):
    def test_event_is_big_pk_ciphertext_only_and_has_no_provider_user_id(self):
        fields = {field.name: field for field in TranscriptEvent._meta.fields}

        self.assertIsInstance(fields["id"], models.BigAutoField)
        self.assertFalse(fields["text_enc"].null)
        self.assertNotIn("text", fields)
        self.assertNotIn("channel_user_id", fields)

    def test_quarantine_has_no_text_field_of_any_kind(self):
        field_names = {field.name for field in TranscriptCaptureQuarantine._meta.fields}

        self.assertFalse(any("text" in name for name in field_names))

    def test_identity_constraint_uses_source_revision_not_content_hash(self):
        constraint = next(
            constraint
            for constraint in TranscriptEvent._meta.constraints
            if constraint.name == "uq_tx_event_source_revision"
        )

        self.assertEqual(
            tuple(constraint.fields),
            ("tenant", "source_type", "source_event_id", "revision"),
        )
