"""Wave 1 "legal but wrong" schedule intake — the tenant-aware half.

These are the inputs the OpenClaw gateway ACCEPTS and then executes wrongly, so
nothing downstream ever reports a failure. Django is the only place that can
refuse them on the typed path, and each case here is anchored to observed
behaviour in openclaw@2026.5.28 / croner 10.0.1:

  - ``everyMs: 3600`` (seconds mistaken for milliseconds) — the runtime clamps
    with ``Math.max(1, ...)``, so this fires a full agent turn every 3.6s.
  - ``at: "20m"`` — the gateway's duration parser is CLI-only; the manual
    taught this shape anyway. We translate it instead of rejecting it.
  - ``at`` with no offset — the runtime appends a ``Z``, silently reinterpreting
    a Tokyo user's 09:00 as 18:00 local.
  - ``kind:"cron"`` with no ``tz`` — evaluated in the container host zone (UTC).
  - ``Etc/GMT+9`` — POSIX sign inversion; it is UTC−9, not Tokyo.

Each fix also emits a telemetry reason code, so the next drift shows up as a
number rather than as a user complaint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from django.test import TestCase

from apps.cron.models import CronPattern
from apps.cron.services import TypedCronError, create_typed_cron
from apps.platform_logs.models import ToolContractEvent
from apps.tenants.models import Tenant, User

_FROZEN_NOW = datetime(2026, 8, 19, 3, 0, 0, tzinfo=UTC)


def _make_tenant(timezone_name: str = "Asia/Tokyo"):
    user = User.objects.create_user(username="cronwave1", password="x", timezone=timezone_name)
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_id="oc-test",
        container_fqdn="oc-test.internal.azurecontainerapps.io",
        postgres_cron_canonical=False,  # off → no QStash regen enqueue
    )


def _reasons(outcome: str) -> list[str]:
    return list(
        ToolContractEvent.objects.filter(namespace="cron", outcome=outcome).values_list("reason_code", flat=True)
    )


class EveryMsFloorTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()

    def test_seconds_mistaken_for_milliseconds_is_rejected(self):
        with self.assertRaises(TypedCronError) as cm:
            create_typed_cron(
                tenant=self.tenant,
                pattern=CronPattern.PURE_REMINDER,
                typed_payload={"text": "x"},
                name="spin",
                schedule={"kind": "every", "everyMs": 3600},
            )

        self.assertEqual(cm.exception.code, "everyms_too_small")
        self.assertIn("MILLISECONDS", str(cm.exception))
        self.assertEqual(_reasons("rejected"), ["everyms_too_small"])

    def test_a_genuinely_hourly_interval_still_works(self):
        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "x"},
            name="hourly",
            schedule={"kind": "every", "everyMs": 3_600_000},
        )

        cron.refresh_from_db()
        self.assertEqual(cron.data["schedule"]["everyMs"], 3_600_000)
        self.assertEqual(_reasons("rejected"), [])


class RelativeAtTranslationTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()

    @patch("apps.cron.services._push_at_cron_immediately")
    def test_remind_me_in_20_minutes_is_stored_as_an_absolute_time(self, _push):
        with patch("django.utils.timezone.now", return_value=_FROZEN_NOW):
            cron = create_typed_cron(
                tenant=self.tenant,
                pattern=CronPattern.PURE_REMINDER,
                typed_payload={"text": "Take out the laundry"},
                name="laundry",
                schedule={"kind": "at", "at": "20m"},
            )

        cron.refresh_from_db()
        self.assertEqual(cron.data["schedule"]["at"], "2026-08-19T03:20:00+00:00")
        self.assertEqual(_reasons("normalized"), ["relative_at_translated"])

    @patch("apps.cron.services._push_at_cron_immediately")
    def test_hours_and_days_translate_too(self, _push):
        cases = [("2h", "2026-08-19T05:00:00+00:00"), ("1d", "2026-08-20T03:00:00+00:00")]
        for index, (relative, expected) in enumerate(cases):
            with self.subTest(relative=relative), patch("django.utils.timezone.now", return_value=_FROZEN_NOW):
                cron = create_typed_cron(
                    tenant=self.tenant,
                    pattern=CronPattern.PURE_REMINDER,
                    typed_payload={"text": "x"},
                    name=f"rel-{index}",
                    schedule={"kind": "at", "at": relative},
                )
            cron.refresh_from_db()
            self.assertEqual(cron.data["schedule"]["at"], expected)

    @patch("apps.cron.services._push_at_cron_immediately")
    def test_an_absolute_timestamp_is_stored_untouched(self, _push):
        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "x"},
            name="absolute",
            schedule={"kind": "at", "at": "2099-01-01T15:00:00+09:00"},
        )

        cron.refresh_from_db()
        self.assertEqual(cron.data["schedule"]["at"], "2099-01-01T15:00:00+09:00")
        self.assertEqual(_reasons("normalized"), [])


class NaiveAtRejectionTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()

    def test_offsetless_timestamp_is_rejected_with_the_shift_explained(self):
        with self.assertRaises(TypedCronError) as cm:
            create_typed_cron(
                tenant=self.tenant,
                pattern=CronPattern.PURE_REMINDER,
                typed_payload={"text": "x"},
                name="naive",
                schedule={"kind": "at", "at": "2099-06-18T09:00:00"},
            )

        self.assertEqual(cm.exception.code, "naive_at_rejected")
        self.assertIn("no timezone offset", str(cm.exception))
        self.assertEqual(_reasons("rejected"), ["naive_at_rejected"])


class CronTimezoneBackfillTests(TestCase):
    def test_omitted_tz_takes_the_tenants_own_timezone(self):
        tenant = _make_tenant("Asia/Tokyo")
        cron = create_typed_cron(
            tenant=tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "x"},
            name="morning",
            schedule={"kind": "cron", "expr": "0 7 * * *"},
        )

        cron.refresh_from_db()
        self.assertEqual(cron.data["schedule"]["tz"], "Asia/Tokyo")
        self.assertEqual(_reasons("normalized"), ["tz_backfilled"])

        # Both detail keys must be ALLOWLISTED in apps/platform_logs/telemetry.py
        # — an unlisted key is dropped silently at write time, which would make
        # the event useless without failing anything.
        event = ToolContractEvent.objects.get(namespace="cron", outcome="normalized")
        self.assertEqual(event.detail, {"schedule_kind": "cron", "pattern": "pure_reminder"})
        self.assertEqual(event.tenant_id, tenant.id)
        self.assertEqual(event.tool_name, "cron-create-pure_reminder")

    def test_explicit_tz_wins_over_the_tenant_default(self):
        tenant = _make_tenant("Asia/Tokyo")
        cron = create_typed_cron(
            tenant=tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "x"},
            name="ny-morning",
            schedule={"kind": "cron", "expr": "0 7 * * *", "tz": "America/New_York"},
        )

        cron.refresh_from_db()
        self.assertEqual(cron.data["schedule"]["tz"], "America/New_York")
        self.assertEqual(_reasons("normalized"), [])

    def test_etc_timezone_is_rejected_for_inverting_the_sign(self):
        tenant = _make_tenant("Asia/Tokyo")
        with self.assertRaises(TypedCronError) as cm:
            create_typed_cron(
                tenant=tenant,
                pattern=CronPattern.PURE_REMINDER,
                typed_payload={"text": "x"},
                name="inverted",
                schedule={"kind": "cron", "expr": "0 7 * * *", "tz": "Etc/GMT+9"},
            )

        self.assertEqual(cm.exception.code, "tz_etc_rejected")
        self.assertEqual(_reasons("rejected"), ["tz_etc_rejected"])

    def test_a_profile_holding_a_bad_zone_backfills_utc_not_a_400(self):
        """Profiles written before the timezone gate must not break cron creation."""
        tenant = _make_tenant("Asia/Tokyo")
        tenant.user.timezone = "Etc/GMT+9"
        tenant.user.save(update_fields=["timezone"])

        cron = create_typed_cron(
            tenant=tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "x"},
            name="legacy-profile",
            schedule={"kind": "cron", "expr": "0 7 * * *"},
        )

        cron.refresh_from_db()
        self.assertEqual(cron.data["schedule"]["tz"], "UTC")


class DayOfWeekRangeTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()

    def test_day_eight_is_rejected_naming_both_conventions(self):
        with self.assertRaises(TypedCronError) as cm:
            create_typed_cron(
                tenant=self.tenant,
                pattern=CronPattern.PURE_REMINDER,
                typed_payload={"text": "x"},
                name="day-eight",
                schedule={"kind": "cron", "expr": "0 9 * * 8", "tz": "Asia/Tokyo"},
            )

        self.assertEqual(cm.exception.code, "dow_out_of_range")
        self.assertEqual(_reasons("rejected"), ["dow_out_of_range"])

    def test_named_days_are_accepted(self):
        """croner 10.0.1 resolves MON-FRI; verified against the shipped package."""
        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "x"},
            name="weekdays",
            schedule={"kind": "cron", "expr": "0 8 * * MON-FRI", "tz": "Asia/Tokyo"},
        )

        cron.refresh_from_db()
        self.assertEqual(cron.data["schedule"]["expr"], "0 8 * * MON-FRI")
