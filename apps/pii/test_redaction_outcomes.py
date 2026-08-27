import concurrent.futures
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.pii.redactor import (
    ConfirmedRedaction,
    RedactionOutcome,
    as_confirmed,
    confirm_assistant_output,
    confirmed_from_receipt_row,
    redact_user_message,
    redact_user_message_checked,
    redaction_receipt,
)
from apps.tenants.models import Tenant, User


class RedactionOutcomeTest(SimpleTestCase):
    def setUp(self):
        self.tenant = SimpleNamespace(model_tier="starter")

    def test_completed_redaction_is_confirmed(self):
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=lambda _text: []),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            outcome = redact_user_message_checked("hello world", self.tenant)

        self.assertEqual(outcome, RedactionOutcome("hello world", True, "redacted"))

    @patch("apps.pii.redactor._redact_user_message", return_value="hello world")
    def test_unset_neural_outcome_is_unavailable(self, _redact):
        outcome = redact_user_message_checked("hello world", self.tenant)

        self.assertEqual(outcome, RedactionOutcome("hello world", False, "neural-unavailable"))

    def test_same_thread_success_failure_success_resets_outcome(self):
        calls = iter((True, False, True))

        def pipeline(_text):
            if next(calls):
                return []
            raise RuntimeError("synthetic unavailable")

        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=pipeline),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            outcomes = [redact_user_message_checked(f"message {index}", self.tenant) for index in range(3)]

        self.assertEqual([outcome.confirmed for outcome in outcomes], [True, False, True])
        self.assertEqual([outcome.reason for outcome in outcomes], ["redacted", "neural-unavailable", "redacted"])

    def test_thread_local_outcomes_do_not_leak_across_threads(self):
        barrier = threading.Barrier(8)

        def pipeline(text):
            barrier.wait()
            if text.startswith("failure"):
                raise RuntimeError("synthetic unavailable")
            return []

        def redact(index):
            kind = "failure" if index % 2 else "success"
            return kind, redact_user_message_checked(f"{kind} {index}", self.tenant)

        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=pipeline),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
            concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor,
        ):
            outcomes = list(executor.map(redact, range(8)))

        for kind, outcome in outcomes:
            self.assertEqual(outcome.confirmed, kind == "success")
            self.assertEqual(outcome.reason, "redacted" if kind == "success" else "neural-unavailable")

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


class NeuralUnavailableOutcomeTest(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="neural-outcome", password="x")
        self.tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)

    def test_checked_api_preserves_presidio_text_but_is_unconfirmed_for_both_transports(self):
        text = "Email private@example.com today"
        for transport in ("local", "shared"):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                self.tenant.pii_entity_map = {}
                self.tenant.pii_type_counters = {}
                self.tenant.save(update_fields=["pii_entity_map", "pii_type_counters"])
                environment = {
                    "PII_DETECTOR_TRANSPORT": transport,
                    "PII_DETECTOR_ENGINE": "deberta",
                    "PII_SHARED_SOCKET": str(Path(directory) / "missing.sock"),
                }
                with patch.dict(os.environ, environment):
                    if transport == "local":
                        model = patch(
                            "apps.pii.engine.get_deberta_pii_pipeline",
                            side_effect=RuntimeError("synthetic unavailable"),
                        )
                    else:
                        model = patch("apps.pii.engine.get_deberta_pii_pipeline")
                    with model as local_loader:
                        outcome = redact_user_message_checked(text, self.tenant)
                        legacy_text = redact_user_message(text, self.tenant)

                self.assertEqual(outcome.text, "Email [EMAIL_ADDRESS_1] today")
                self.assertEqual(legacy_text, outcome.text)
                self.assertFalse(outcome.confirmed)
                self.assertEqual(outcome.reason, "neural-unavailable")
                if transport == "shared":
                    local_loader.assert_not_called()

    def test_successful_neural_call_keeps_confirmed_receipt(self):
        with (
            patch.dict(os.environ, {"PII_DETECTOR_TRANSPORT": "local"}),
            patch("apps.pii.engine.get_deberta_pii_pipeline", return_value=lambda _text: []),
        ):
            outcome = redact_user_message_checked("Email private@example.com", self.tenant)

        self.assertEqual(outcome.text, "Email [EMAIL_ADDRESS_1]")
        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.reason, "redacted")


class ConfirmedRedactionTest(SimpleTestCase):
    def test_as_confirmed_mints_only_for_literal_confirmed_outcome(self):
        receipt = redaction_receipt({"redaction": {"confirmed": True, "reason": "redacted"}})
        confirmed = as_confirmed(receipt)

        self.assertIsInstance(confirmed, ConfirmedRedaction)
        self.assertEqual(confirmed.text, "")
        self.assertIsNone(as_confirmed(RedactionOutcome("hello Alice", False, "redaction-error")))
        self.assertIsNone(as_confirmed(redaction_receipt({})))

    def test_confirmed_from_receipt_row_owns_receipt_text_pairing(self):
        confirmed = confirmed_from_receipt_row(
            {"redaction": {"confirmed": True, "reason": "redacted"}},
            "stored [PERSON_1]",
        )

        self.assertIsInstance(confirmed, ConfirmedRedaction)
        self.assertEqual(confirmed.text, "stored [PERSON_1]")
        self.assertEqual(confirmed.reason, "redacted")
        self.assertIsNone(
            confirmed_from_receipt_row(
                {"redaction": {"confirmed": False, "reason": "redaction-error"}},
                "raw Alice",
            )
        )
        self.assertIsNone(confirmed_from_receipt_row(None, "legacy raw Alice"))

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
