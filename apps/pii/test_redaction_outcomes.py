from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.pii.redactor import (
    ConfirmedRedaction,
    RedactionOutcome,
    as_confirmed,
    confirm_assistant_output,
    redact_user_message,
    redact_user_message_checked,
    redaction_receipt,
)


class RedactionOutcomeTest(SimpleTestCase):
    def setUp(self):
        self.tenant = SimpleNamespace(model_tier="starter")

    @patch("apps.pii.redactor._redact_user_message", return_value="hello [PERSON_1]")
    def test_completed_redaction_is_confirmed(self, _redact):
        outcome = redact_user_message_checked("hello Alice", self.tenant)

        self.assertEqual(outcome, RedactionOutcome("hello [PERSON_1]", True, "redacted"))

    def test_disabled_redaction_is_unconfirmed_and_fail_open(self):
        disabled = {"enabled": False, "entities": []}
        with patch.dict("apps.pii.redactor.TIER_POLICIES", {"starter": disabled}, clear=True):
            outcome = redact_user_message_checked("hello Alice", self.tenant)
            legacy_text = redact_user_message("hello Alice", self.tenant)

        self.assertEqual(outcome, RedactionOutcome("hello Alice", False, "redaction-disabled"))
        self.assertEqual(legacy_text, "hello Alice")

    @patch("apps.pii.redactor._redact_user_message", side_effect=RuntimeError("detector unavailable"))
    def test_exception_is_unconfirmed_and_legacy_api_stays_fail_open(self, _redact):
        outcome = redact_user_message_checked("hello Alice", self.tenant)
        legacy_text = redact_user_message("hello Alice", self.tenant)

        self.assertEqual(outcome, RedactionOutcome("hello Alice", False, "redaction-error"))
        self.assertEqual(legacy_text, "hello Alice")

    def test_absent_receipt_is_a_pre_receipt_row(self):
        self.assertEqual(
            redaction_receipt({"message_text": "legacy"}),
            RedactionOutcome(text="", confirmed=False, reason="pre-receipt-row"),
        )

    def test_reader_requires_literal_true(self):
        outcome = redaction_receipt(
            {
                "redaction": {
                    "confirmed": 1,
                    "reason": "redacted",
                }
            }
        )
        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.reason, "redacted")


class ConfirmedRedactionTest(SimpleTestCase):
    def test_as_confirmed_mints_only_for_literal_confirmed_outcome(self):
        receipt = redaction_receipt({"redaction": {"confirmed": True, "reason": "redacted"}})
        confirmed = as_confirmed(receipt)

        self.assertIsInstance(confirmed, ConfirmedRedaction)
        self.assertEqual(confirmed.text, "")
        self.assertIsNone(as_confirmed(RedactionOutcome("hello Alice", False, "redaction-error")))
        self.assertIsNone(as_confirmed(redaction_receipt({})))

    def test_assistant_confirmation_scrubs_known_values(self):
        tenant = SimpleNamespace(
            id="tenant-confirm",
            pii_entity_map={"[PERSON_1]": {"name": "Theo Smith"}},
        )

        confirmed = confirm_assistant_output(tenant, "Ask Theo Smith tomorrow")

        self.assertIsInstance(confirmed, ConfirmedRedaction)
        self.assertEqual(confirmed.text, "Ask [PERSON_1] tomorrow")

    @patch("apps.pii.egress._redact_known_values", side_effect=RuntimeError("boom"))
    def test_assistant_confirmation_refuses_to_mint_on_scrub_failure(self, _scrub):
        tenant = SimpleNamespace(id="tenant-confirm", pii_entity_map={"[PERSON_1]": "Theo Smith"})

        self.assertIsNone(confirm_assistant_output(tenant, "Theo Smith"))
