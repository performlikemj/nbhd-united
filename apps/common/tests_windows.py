"""Tests for ``apps.common.windows`` — window validation + date-interval resolution."""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime
from unittest import TestCase

from pydantic import ValidationError

from apps.common.windows import Window, resolve_window

# ─── Schema validation (Window.kind + .value pairing) ──────────────────────


class WindowValidationTests(TestCase):
    def test_kind_with_no_value_required(self):
        for kind in (
            "today",
            "yesterday",
            "tomorrow",
            "all",
            "this_week",
            "last_week",
            "month_to_date",
            "last_month",
            "year_to_date",
            "last_year",
        ):
            w = Window(kind=kind)
            self.assertEqual(w.kind, kind)
            self.assertIsNone(w.value)

    def test_kind_no_value_rejects_extraneous_value(self):
        with self.assertRaises(ValidationError):
            Window(kind="today", value=1)

    def test_last_n_days_requires_positive_int(self):
        Window(kind="last_n_days", value=7)
        with self.assertRaises(ValidationError):
            Window(kind="last_n_days")
        with self.assertRaises(ValidationError):
            Window(kind="last_n_days", value=0)
        with self.assertRaises(ValidationError):
            Window(kind="last_n_days", value=731)
        with self.assertRaises(ValidationError):
            Window(kind="last_n_days", value="seven")  # type: ignore[arg-type]

    def test_last_n_weeks_caps_at_104(self):
        Window(kind="last_n_weeks", value=104)
        with self.assertRaises(ValidationError):
            Window(kind="last_n_weeks", value=105)

    def test_last_n_months_caps_at_24(self):
        Window(kind="last_n_months", value=24)
        with self.assertRaises(ValidationError):
            Window(kind="last_n_months", value=25)

    def test_since_requires_date(self):
        Window(kind="since", value=date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            Window(kind="since")
        with self.assertRaises(ValidationError):
            Window(kind="since", value=7)
        with self.assertRaises(ValidationError):
            Window(kind="since", value=datetime(2026, 1, 1, 12, 0))  # type: ignore[arg-type]

    def test_between_requires_two_dates_in_order(self):
        Window(kind="between", value=[date(2026, 1, 1), date(2026, 1, 31)])
        with self.assertRaises(ValidationError):
            Window(kind="between")
        with self.assertRaises(ValidationError):
            Window(kind="between", value=[date(2026, 1, 1)])
        with self.assertRaises(ValidationError):
            Window(kind="between", value=[date(2026, 1, 31), date(2026, 1, 1)])

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValidationError):
            Window(kind="last_quarter")  # type: ignore[arg-type]


# ─── Resolution: single-day kinds ──────────────────────────────────────────


class WindowResolutionSingleDayTests(TestCase):
    def setUp(self):
        # Wednesday May 20 2026 14:00 in Tokyo (= 05:00 UTC)
        self.now = datetime(2026, 5, 20, 14, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo"))

    def test_today(self):
        self.assertEqual(
            resolve_window(Window(kind="today"), "Asia/Tokyo", now=self.now),
            (date(2026, 5, 20), date(2026, 5, 20)),
        )

    def test_yesterday(self):
        self.assertEqual(
            resolve_window(Window(kind="yesterday"), "Asia/Tokyo", now=self.now),
            (date(2026, 5, 19), date(2026, 5, 19)),
        )

    def test_tomorrow(self):
        self.assertEqual(
            resolve_window(Window(kind="tomorrow"), "Asia/Tokyo", now=self.now),
            (date(2026, 5, 21), date(2026, 5, 21)),
        )

    def test_all_returns_none(self):
        self.assertIsNone(resolve_window(Window(kind="all"), "Asia/Tokyo", now=self.now))


# ─── Resolution: trailing-N kinds ──────────────────────────────────────────


class WindowResolutionTrailingTests(TestCase):
    NOW = datetime(2026, 5, 20, 14, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo"))

    def test_last_n_days_1_is_today_only(self):
        self.assertEqual(
            resolve_window(Window(kind="last_n_days", value=1), "Asia/Tokyo", now=self.NOW),
            (date(2026, 5, 20), date(2026, 5, 20)),
        )

    def test_last_n_days_7_includes_today(self):
        self.assertEqual(
            resolve_window(Window(kind="last_n_days", value=7), "Asia/Tokyo", now=self.NOW),
            (date(2026, 5, 14), date(2026, 5, 20)),  # 7 days inclusive
        )

    def test_last_n_days_30(self):
        self.assertEqual(
            resolve_window(Window(kind="last_n_days", value=30), "Asia/Tokyo", now=self.NOW),
            (date(2026, 4, 21), date(2026, 5, 20)),  # 30 days inclusive
        )

    def test_next_n_days_7_starts_today(self):
        self.assertEqual(
            resolve_window(Window(kind="next_n_days", value=7), "Asia/Tokyo", now=self.NOW),
            (date(2026, 5, 20), date(2026, 5, 26)),
        )

    def test_last_n_weeks_equals_last_n_days_times_7(self):
        a = resolve_window(Window(kind="last_n_weeks", value=2), "Asia/Tokyo", now=self.NOW)
        b = resolve_window(Window(kind="last_n_days", value=14), "Asia/Tokyo", now=self.NOW)
        self.assertEqual(a, b)
        self.assertEqual(a, (date(2026, 5, 7), date(2026, 5, 20)))

    def test_last_n_months_1_is_same_day_one_month_ago_plus_one(self):
        # NOW = 2026-05-20. One month back = 2026-04-20. Then +1 day = 2026-04-21.
        self.assertEqual(
            resolve_window(Window(kind="last_n_months", value=1), "Asia/Tokyo", now=self.NOW),
            (date(2026, 4, 21), date(2026, 5, 20)),
        )

    def test_last_n_months_clamps_to_short_month(self):
        # 2026-03-31 minus 1 month would be 2026-02-31 (invalid) → clamp to 2026-02-28 → from = 2026-03-01.
        now = datetime(2026, 3, 31, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="last_n_months", value=1), "UTC", now=now),
            (date(2026, 3, 1), date(2026, 3, 31)),
        )

    def test_last_n_months_leap_year_clamp(self):
        # 2024 is a leap year. 2024-03-30 minus 1 month → 2024-02-29 → from = 2024-03-01.
        now = datetime(2024, 3, 30, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="last_n_months", value=1), "UTC", now=now),
            (date(2024, 3, 1), date(2024, 3, 30)),
        )

    def test_last_n_months_crosses_year_boundary(self):
        # 2026-01-15 minus 3 months = 2025-10-15 → from = 2025-10-16.
        now = datetime(2026, 1, 15, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="last_n_months", value=3), "UTC", now=now),
            (date(2025, 10, 16), date(2026, 1, 15)),
        )


# ─── Resolution: bucketed (week/month/year) kinds ──────────────────────────


class WindowResolutionBucketedTests(TestCase):
    def test_this_week_wednesday(self):
        # Wed 2026-05-20 — ISO Monday = 2026-05-18, Sunday = 2026-05-24
        now = datetime(2026, 5, 20, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="this_week"), "UTC", now=now),
            (date(2026, 5, 18), date(2026, 5, 24)),
        )

    def test_this_week_when_today_is_monday(self):
        now = datetime(2026, 5, 18, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="this_week"), "UTC", now=now),
            (date(2026, 5, 18), date(2026, 5, 24)),
        )

    def test_this_week_when_today_is_sunday(self):
        now = datetime(2026, 5, 24, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="this_week"), "UTC", now=now),
            (date(2026, 5, 18), date(2026, 5, 24)),
        )

    def test_last_week(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="last_week"), "UTC", now=now),
            (date(2026, 5, 11), date(2026, 5, 17)),
        )

    def test_month_to_date_mid_month(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="month_to_date"), "UTC", now=now),
            (date(2026, 5, 1), date(2026, 5, 20)),
        )

    def test_month_to_date_first_of_month(self):
        now = datetime(2026, 5, 1, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="month_to_date"), "UTC", now=now),
            (date(2026, 5, 1), date(2026, 5, 1)),
        )

    def test_last_month_in_may_returns_april(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="last_month"), "UTC", now=now),
            (date(2026, 4, 1), date(2026, 4, 30)),
        )

    def test_last_month_january_crosses_year(self):
        now = datetime(2026, 1, 15, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="last_month"), "UTC", now=now),
            (date(2025, 12, 1), date(2025, 12, 31)),
        )

    def test_year_to_date(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="year_to_date"), "UTC", now=now),
            (date(2026, 1, 1), date(2026, 5, 20)),
        )

    def test_last_year(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="last_year"), "UTC", now=now),
            (date(2025, 1, 1), date(2025, 12, 31)),
        )


# ─── Resolution: absolute (since / between) ────────────────────────────────


class WindowResolutionAbsoluteTests(TestCase):
    NOW = datetime(2026, 5, 20, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))

    def test_since(self):
        self.assertEqual(
            resolve_window(
                Window(kind="since", value=date(2026, 5, 1)),
                "UTC",
                now=self.NOW,
            ),
            (date(2026, 5, 1), date(2026, 5, 20)),
        )

    def test_between(self):
        self.assertEqual(
            resolve_window(
                Window(kind="between", value=[date(2026, 5, 1), date(2026, 5, 15)]),
                "UTC",
                now=self.NOW,
            ),
            (date(2026, 5, 1), date(2026, 5, 15)),
        )


# ─── Timezone correctness ──────────────────────────────────────────────────


class WindowTimezoneTests(TestCase):
    def test_midnight_crossover_in_tokyo(self):
        # 2026-05-20 23:59 in Tokyo is still 2026-05-20 there, even though UTC
        # has already rolled to 14:59 (Tokyo is UTC+9).
        now = datetime(2026, 5, 20, 14, 59, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="today"), "Asia/Tokyo", now=now),
            (date(2026, 5, 20), date(2026, 5, 20)),
        )

    def test_today_in_tokyo_vs_utc_when_just_past_midnight_tokyo(self):
        # 2026-05-20 15:00 UTC = 2026-05-21 00:00 Tokyo (just rolled).
        now = datetime(2026, 5, 20, 15, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="today"), "Asia/Tokyo", now=now),
            (date(2026, 5, 21), date(2026, 5, 21)),
        )
        self.assertEqual(
            resolve_window(Window(kind="today"), "UTC", now=now),
            (date(2026, 5, 20), date(2026, 5, 20)),
        )

    def test_dst_spring_forward_ny(self):
        # 2026-03-08 02:30 EST doesn't exist (jumps to 03:30 EDT). We don't
        # touch time-of-day in resolution — just dates — so this is moot,
        # but ensure the date-side math survives the date crossing.
        # Use a moment safely past the DST boundary.
        now = datetime(2026, 3, 8, 7, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))  # 03:00 EDT
        self.assertEqual(
            resolve_window(Window(kind="today"), "America/New_York", now=now),
            (date(2026, 3, 8), date(2026, 3, 8)),
        )

    def test_dst_fall_back_ny(self):
        # 2026-11-01 06:00 UTC = 01:00 EST or 02:00 EDT depending on the rollover.
        now = datetime(2026, 11, 1, 6, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="today"), "America/New_York", now=now),
            (date(2026, 11, 1), date(2026, 11, 1)),
        )

    def test_invalid_tz_falls_back_to_utc(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(
            resolve_window(Window(kind="today"), "Mars/Phobos", now=now),
            (date(2026, 5, 20), date(2026, 5, 20)),
        )

    def test_naive_now_is_interpreted_in_supplied_tz(self):
        naive = datetime(2026, 5, 20, 23, 30)  # interpreted in Tokyo
        self.assertEqual(
            resolve_window(Window(kind="today"), "Asia/Tokyo", now=naive),
            (date(2026, 5, 20), date(2026, 5, 20)),
        )
