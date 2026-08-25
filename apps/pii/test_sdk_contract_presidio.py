"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from django.test import SimpleTestCase
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    PhoneRecognizer,
)


class PresidioSdkContractTest(SimpleTestCase):
    def test_recognizers_construct_and_accept_our_analyze_kwargs(self):
        recognizers = (
            CreditCardRecognizer(),
            EmailRecognizer(),
            IbanRecognizer(),
            PhoneRecognizer(supported_regions=("US", "GB", "JP")),
        )

        for recognizer in recognizers:
            inspect.signature(recognizer.analyze).bind(
                text="person@example.test", entities=recognizer.supported_entities
            )

    def test_email_recognizer_exposes_mutable_regex_flags(self):
        recognizer = EmailRecognizer()

        self.assertIsInstance(recognizer.global_regex_flags, int)
