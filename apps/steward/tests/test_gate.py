from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.db import OperationalError, close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.steward.gate import record_sent, record_suppressed, should_send
from apps.steward.models import AlertState


class AlertGateTests(TestCase):
    def test_grants_suppresses_then_regrants_after_cooldown(self):
        now = timezone.now()
        with patch("apps.steward.gate.timezone.now", return_value=now):
            self.assertTrue(should_send("gate:test", timedelta(hours=1)))
            self.assertTrue(should_send("gate:test", timedelta(hours=1)))
            record_sent("gate:test")
            self.assertFalse(should_send("gate:test", timedelta(hours=1)))
            record_suppressed("gate:test")
        with patch(
            "apps.steward.gate.timezone.now",
            return_value=now + timedelta(hours=1),
        ):
            self.assertTrue(should_send("gate:test", timedelta(hours=1)))

        state = AlertState.objects.get(fingerprint="gate:test")
        self.assertEqual(state.sent_count, 1)
        self.assertEqual(state.suppressed_count, 1)

    @patch(
        "apps.steward.gate.AlertState.objects.filter",
        side_effect=OperationalError("missing table"),
    )
    def test_database_unavailable_fails_open(self, _filter):
        self.assertTrue(should_send("gate:missing", timedelta(hours=1)))


class AlertGateConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    @staticmethod
    def _attempt() -> bool:
        close_old_connections()
        try:
            return should_send("gate:concurrent", timedelta(hours=1))
        finally:
            close_old_connections()

    def test_unconfirmed_concurrent_checks_may_both_send(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self._attempt(), range(2)))
        self.assertEqual(results, [True, True])
        self.assertFalse(AlertState.objects.filter(fingerprint="gate:concurrent").exists())
