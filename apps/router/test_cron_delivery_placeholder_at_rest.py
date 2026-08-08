"""P3 W3b DeliveryAttempt excerpt authoring and final-receipt coverage."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

from django.test import TestCase
from rest_framework.response import Response

from apps.tenants.services import create_tenant

from .cron_delivery import _resolve_delivery_attempt
from .models import DeliveryAttempt


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


class CronDeliveryPlaceholderTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="W3b Cron", telegram_chat_id=880317)
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])

    def _attempt(self, suffix):
        return DeliveryAttempt.objects.create(
            tenant=self.tenant,
            occurrence_key=f"w3b-{suffix}",
            job_name="w3b",
            channel="app",
        )

    def _enable_placeholder_writes(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

    def test_flag_off_real_resolver_preserves_legacy_excerpt_bytes(self):
        attempt = self._attempt("off")
        response = Response({"detail": "opaque failure bytes"}, status=503)
        expected = json.dumps(response.data, sort_keys=True, default=str)[:500]

        _resolve_delivery_attempt(attempt, state=DeliveryAttempt.State.FAILED, response=response)

        attempt.refresh_from_db()
        self.assertEqual(attempt.response_excerpt, expected)
        self.assertEqual(
            attempt.pii_receipts["response_excerpt"],
            {"state": "bypass", "writer": "background"},
        )

    def test_flag_on_excerpt_stores_placeholder_and_background_receipt(self):
        self._enable_placeholder_writes()
        attempt = self._attempt("on")
        with _checked_detection():
            _resolve_delivery_attempt(
                attempt,
                state=DeliveryAttempt.State.AMBIGUOUS,
                excerpt="Delivery to Alice timed out",
            )

        attempt.refresh_from_db()
        self.assertEqual(attempt.response_excerpt, "Delivery to [PERSON_1] timed out")
        receipt = attempt.pii_receipts["response_excerpt"]
        self.assertEqual(receipt["writer"], "background")
        self.assertEqual(receipt["redactions"], [{"placeholder": "[PERSON_1]"}])

    def test_flag_on_response_slices_to_500_before_authoring_and_truncates_growth_safely(self):
        self._enable_placeholder_writes()
        attempt = self._attempt("boundary")
        empty_rendered = json.dumps({"detail": ""}, sort_keys=True, default=str)
        value_prefix_len = empty_rendered.index('""') + 1
        padding = 494 - value_prefix_len
        response = Response({"detail": ("x" * padding) + " Alice"}, status=503)
        rendered = json.dumps(response.data, sort_keys=True, default=str)
        self.assertEqual(rendered.index("Alice"), 495)

        detector_inputs = []

        def bounded_detector(text, *_args, **_kwargs):
            detector_inputs.append(text)
            return []

        with (
            patch("apps.pii.redactor._detect_pii", side_effect=bounded_detector),
            patch("apps.pii.authoring._detect_pii", side_effect=bounded_detector),
        ):
            _resolve_delivery_attempt(attempt, state=DeliveryAttempt.State.FAILED, response=response)

        attempt.refresh_from_db()
        self.assertEqual(attempt.response_excerpt, rendered[:495])
        self.assertLessEqual(len(attempt.response_excerpt), 500)
        self.assertNotIn("[PERS", attempt.response_excerpt)
        self.assertTrue(detector_inputs)
        self.assertTrue(all(len(text) <= 500 for text in detector_inputs))
        receipt = attempt.pii_receipts["response_excerpt"]
        self.assertEqual(receipt["writer"], "background")
        self.assertEqual(receipt["redactions"], [])
