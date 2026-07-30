from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.db import OperationalError, close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.steward.gate import should_send
from apps.steward.models import AlertState


class AlertGateTests(TestCase):
    def test_grants_suppresses_then_regrants_after_cooldown(self):
        now = timezone.now()
        with patch("apps.steward.gate.timezone.now", return_value=now):
            self.assertTrue(should_send("gate:test", timedelta(hours=1)))
            self.assertFalse(should_send("gate:test", timedelta(hours=1)))
        with patch(
            "apps.steward.gate.timezone.now",
            return_value=now + timedelta(hours=1),
        ):
            self.assertTrue(should_send("gate:test", timedelta(hours=1)))

        state = AlertState.objects.get(fingerprint="gate:test")
        self.assertEqual(state.sent_count, 2)

    @patch(
        "apps.steward.gate._locked_state",
        side_effect=OperationalError("missing table"),
    )
    def test_database_unavailable_fails_open(self, _locked):
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

    def test_two_concurrent_attempts_grant_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self._attempt(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(
            AlertState.objects.get(fingerprint="gate:concurrent").sent_count,
            1,
        )
