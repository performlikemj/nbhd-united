"""Tests for the sole structured UserSituation write service."""

from __future__ import annotations

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.orchestrator.envelope_registry import suppress_refresh
from apps.tenants.models import Tenant, User, UserSituation
from apps.tenants.situation import record_device_tz, record_place_observation


class SituationServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="situation-service", password="pw")
        self.user.location_city = " Tokyo "
        self.user.save(update_fields=["location_city"])
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            situational_context_enabled=True,
        )
        self.now = timezone.now().replace(microsecond=0)

    def _place(self, label, source="ios_chat", observed_at=None):
        with suppress_refresh():
            return record_place_observation(self.tenant, label, source, observed_at)

    def _device_tz(self, tz_name, source_device="healthkit", observed_at=None):
        with suppress_refresh():
            return record_device_tz(self.tenant, tz_name, source_device, observed_at)

    def test_label_validation_rejects_unsafe_values_and_accepts_clean_label(self):
        rejected = [
            "",
            "   ",
            "a" * 65,
            "Fukuoka\nJapan",
            "Fukuoka\u2028Japan",
            "Fukuoka\x00Japan",
            "#Fukuoka",
            "Fu*kuoka",
            "Fu_kuoka",
            "Fu[kuoka",
            "Fu~kuoka",
        ]
        for label in rejected:
            with self.subTest(label=repr(label)):
                self.assertFalse(self._place(label, observed_at=self.now))
                self.assertFalse(UserSituation.objects.exists())

        self.assertTrue(self._place("  Fukuoka  ", observed_at=self.now))
        situation = UserSituation.objects.get(tenant=self.tenant)
        self.assertEqual(situation.current_place_label, "Fukuoka")
        self.assertEqual(situation.current_place_source, "ios_chat")

    def test_same_label_updates_last_observed_only_and_throttles_writes(self):
        self.assertTrue(self._place("Fukuoka", observed_at=self.now))
        situation = UserSituation.objects.get(tenant=self.tenant)
        original_since = situation.current_place_since
        original_source = situation.current_place_source

        with CaptureQueriesContext(connection) as queries:
            self.assertFalse(self._place("Fukuoka", source="other", observed_at=self.now + timedelta(minutes=5)))
        situation_writes = [
            q["sql"]
            for q in queries.captured_queries
            if "user_situations" in q["sql"] and q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT"))
        ]
        self.assertEqual(situation_writes, [])

        observed_later = self.now + timedelta(minutes=11)
        self.assertFalse(self._place("Fukuoka", source="other", observed_at=observed_later))
        situation.refresh_from_db()
        self.assertEqual(situation.current_place_last_observed_at, observed_later)
        self.assertEqual(situation.current_place_since, original_since)
        self.assertEqual(situation.current_place_source, original_source)

    def test_new_label_resets_since_and_source(self):
        self._place("Fukuoka", observed_at=self.now)
        moved_at = self.now + timedelta(hours=3)
        self.assertTrue(self._place("Osaka", source="ios_chat", observed_at=moved_at))
        situation = UserSituation.objects.get(tenant=self.tenant)
        self.assertEqual(situation.current_place_label, "Osaka")
        self.assertEqual(situation.current_place_since, moved_at)
        self.assertEqual(situation.current_place_last_observed_at, moved_at)

    def test_device_timezone_rejects_out_of_order_observation(self):
        self.assertTrue(self._device_tz("Asia/Tokyo", observed_at=self.now))
        self.assertFalse(self._device_tz("America/New_York", observed_at=self.now - timedelta(seconds=1)))
        situation = UserSituation.objects.get(tenant=self.tenant)
        self.assertEqual(situation.device_tz, "Asia/Tokyo")
        self.assertEqual(situation.device_tz_last_observed_at, self.now)

    def test_invalid_iana_timezone_is_debug_noop(self):
        with self.assertLogs("apps.tenants.situation", level="DEBUG") as logs:
            self.assertFalse(self._device_tz("Not/AZone", observed_at=self.now))
        self.assertIn("situation_device_tz_invalid", "\n".join(logs.output))
        self.assertFalse(UserSituation.objects.exists())

    def test_flag_off_and_eval_sink_do_not_write(self):
        self.tenant.situational_context_enabled = False
        self.assertFalse(self._place("Fukuoka", observed_at=self.now))
        self.assertFalse(self._device_tz("Asia/Tokyo", observed_at=self.now))
        self.assertFalse(UserSituation.objects.exists())

        self.tenant.situational_context_enabled = True
        self.tenant.is_eval_sink = True
        self.assertFalse(self._place("Fukuoka", observed_at=self.now))
        self.assertFalse(self._device_tz("Asia/Tokyo", observed_at=self.now))
        self.assertFalse(UserSituation.objects.exists())

    def test_place_log_reports_differs_home_without_label(self):
        with self.assertLogs("apps.tenants.situation", level="INFO") as same_logs:
            self.assertTrue(self._place("tokyo", observed_at=self.now))
        same_line = "\n".join(same_logs.output)
        self.assertIn("differs_home=0", same_line)
        self.assertNotIn("tokyo", same_line.lower())

        with self.assertLogs("apps.tenants.situation", level="INFO") as away_logs:
            self.assertTrue(self._place("Fukuoka", observed_at=self.now + timedelta(hours=1)))
        away_line = "\n".join(away_logs.output)
        self.assertIn("differs_home=1", away_line)
        self.assertNotIn("fukuoka", away_line.lower())
