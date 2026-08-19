"""Table-driven tests for schedule intake validation."""

from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase

from apps.cron.schedule_validation import (
    MIN_EVERY_MS,
    ScheduleValidationError,
    normalize_schedule,
    parse_relative_at,
    validate_schedule,
)


class ScheduleValidationTests(SimpleTestCase):
    def test_cron_expression_table(self):
        cases = [
            ("DOM and DOW", "0 9 24 7 5", False),
            ("weekly", "0 9 * * 5", True),
            ("monthly", "0 9 24 7 *", True),
            ("welcome", "25 23 25 4 *", True),
            ("fuel", "0 6 * * 1,2,4,6", True),
        ]

        for label, expr, valid in cases:
            with self.subTest(label=label, expr=expr):
                schedule = {"kind": "cron", "expr": expr, "tz": "UTC"}
                if valid:
                    self.assertIsNone(validate_schedule(schedule))
                else:
                    with self.assertRaises(ScheduleValidationError) as cm:
                        validate_schedule(schedule)
                    message = str(cm.exception)
                    self.assertIn("OR-ed", message)
                    self.assertIn("kind:'at'", message)
                    self.assertIn("day-of-week to *", message)
                    self.assertIn("day-of-month to *", message)

    def test_six_field_cron_is_rejected_with_precision_guidance(self):
        with self.assertRaises(ScheduleValidationError) as cm:
            validate_schedule(
                {
                    "kind": "cron",
                    "expr": "0 0 9 * * 5",
                    "tz": "UTC",
                }
            )

        message = str(cm.exception)
        self.assertIn("seconds precision is unsupported", message)
        self.assertIn("use 5 fields", message)

    def test_at_timestamp_table(self):
        cases = [
            ("timezone offset", "2099-01-01T15:00:00+09:00", True),
            ("Zulu", "2099-01-01T06:00:00Z", True),
            ("malformed", "Friday July 24 at 9", False),
        ]

        for label, timestamp, valid in cases:
            with self.subTest(label=label):
                schedule = {"kind": "at", "at": timestamp}
                if valid:
                    self.assertIsNone(validate_schedule(schedule))
                else:
                    with self.assertRaises(ScheduleValidationError) as cm:
                        validate_schedule(schedule)
                    self.assertIn("parseable ISO-8601 timestamp", str(cm.exception))

    def test_naive_at_is_rejected_because_the_gateway_reads_it_as_utc(self):
        """OC appends a Z to any offset-less ISO string — a silent 9h shift for Tokyo."""
        with self.assertRaises(ScheduleValidationError) as cm:
            validate_schedule({"kind": "at", "at": "2099-06-18T09:00:00"})

        self.assertEqual(cm.exception.code, "naive_at_rejected")
        message = str(cm.exception)
        self.assertIn("no timezone offset", message)
        self.assertIn("+09:00", message)
        self.assertIn("20m", message)

    def test_relative_at_is_not_silently_accepted_by_the_pure_validator(self):
        """Only paths that go through normalize_schedule may take a duration."""
        with self.assertRaises(ScheduleValidationError):
            validate_schedule({"kind": "at", "at": "20m"})

    def test_every_ms_floor_table(self):
        cases = [
            ("hourly in ms", 3_600_000, True, None),
            ("exactly one minute", MIN_EVERY_MS, True, None),
            ("hourly mistaken for seconds", 3600, False, "everyms_too_small"),
            ("one second", 1000, False, "everyms_too_small"),
            ("float", 3_600_000.5, False, "everyms_not_integer"),
            ("string", "3600000", False, "everyms_not_integer"),
        ]

        for label, every_ms, valid, code in cases:
            with self.subTest(label=label):
                schedule = {"kind": "every", "everyMs": every_ms}
                if valid:
                    self.assertIsNone(validate_schedule(schedule))
                else:
                    with self.assertRaises(ScheduleValidationError) as cm:
                        validate_schedule(schedule)
                    self.assertEqual(cm.exception.code, code)

    def test_every_ms_too_small_message_teaches_the_unit(self):
        with self.assertRaises(ScheduleValidationError) as cm:
            validate_schedule({"kind": "every", "everyMs": 3600})

        message = str(cm.exception)
        self.assertIn("MILLISECONDS", message)
        self.assertIn("3.6 seconds", message)
        self.assertIn("3600000", message)

    def test_missing_every_ms_still_reports_as_missing(self):
        with self.assertRaises(ScheduleValidationError) as cm:
            validate_schedule({"kind": "every"})
        self.assertIn("requires schedule.everyMs", str(cm.exception))

    def test_cron_timezone_table(self):
        cases = [
            ("Area/Location", "Asia/Tokyo", None),
            ("UTC", "UTC", None),
            ("omitted", None, None),
            ("blank", "   ", None),
            ("POSIX inverted sign", "Etc/GMT+9", "tz_etc_rejected"),
            ("POSIX UTC alias", "Etc/UTC", "tz_etc_rejected"),
            ("nonsense", "Mars/Olympus", "tz_invalid"),
        ]

        for label, tz, code in cases:
            with self.subTest(label=label, tz=tz):
                schedule = {"kind": "cron", "expr": "0 9 * * 1"}
                if tz is not None:
                    schedule["tz"] = tz
                if code is None:
                    self.assertIsNone(validate_schedule(schedule))
                else:
                    with self.assertRaises(ScheduleValidationError) as cm:
                        validate_schedule(schedule)
                    self.assertEqual(cm.exception.code, code)

    def test_etc_timezone_message_names_the_inversion(self):
        with self.assertRaises(ScheduleValidationError) as cm:
            validate_schedule({"kind": "cron", "expr": "0 9 * * 1", "tz": "Etc/GMT+9"})

        message = str(cm.exception)
        self.assertIn("INVERTED", message)
        self.assertIn("Asia/Tokyo", message)

    def test_day_of_week_table(self):
        """Anchored to croner 10.0.1: 0-7 valid (0 and 7 both Sunday), 8 throws."""
        cases = [
            ("Sunday as 0", "0", True),
            ("Sunday as 7", "7", True),
            ("Saturday as 6", "6", True),
            ("weekday range", "1-5", True),
            ("list", "1,3,5", True),
            ("wildcard", "*", True),
            ("step", "*/2", True),
            ("names", "MON-FRI", True),
            ("mixed names", "SUN,WED", True),
            ("out of range", "8", False),
            ("out of range in a list", "1,8", False),
            ("out of range in a range", "1-9", False),
        ]

        for label, dow, valid in cases:
            with self.subTest(label=label, dow=dow):
                schedule = {"kind": "cron", "expr": f"0 9 * * {dow}", "tz": "UTC"}
                if valid:
                    self.assertIsNone(validate_schedule(schedule))
                else:
                    with self.assertRaises(ScheduleValidationError) as cm:
                        validate_schedule(schedule)
                    self.assertEqual(cm.exception.code, "dow_out_of_range")

    def test_day_of_week_message_names_both_conventions(self):
        with self.assertRaises(ScheduleValidationError) as cm:
            validate_schedule({"kind": "cron", "expr": "0 9 * * 8", "tz": "UTC"})

        message = str(cm.exception)
        self.assertIn("0=Sunday", message)
        self.assertIn("0=Monday", message)


class RelativeAtParsingTests(SimpleTestCase):
    def test_parse_table(self):
        cases = [
            ("20m", timedelta(minutes=20)),
            ("2h", timedelta(hours=2)),
            ("1d", timedelta(days=1)),
            ("  90m  ", timedelta(minutes=90)),
            ("2H", timedelta(hours=2)),
            ("20 m", timedelta(minutes=20)),
            ("20s", None),
            ("20", None),
            ("soon", None),
            ("2099-01-01T15:00:00+09:00", None),
            (None, None),
            (1200, None),
        ]

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_relative_at(value), expected)


class NormalizeScheduleTests(SimpleTestCase):
    _NOW = datetime(2026, 8, 19, 3, 0, 0, tzinfo=UTC)

    def test_relative_at_becomes_an_absolute_utc_timestamp(self):
        normalized, reasons = normalize_schedule(
            {"kind": "at", "at": "20m"},
            tz_name="Asia/Tokyo",
            now=self._NOW,
        )

        self.assertEqual(reasons, ["relative_at_translated"])
        self.assertEqual(normalized["at"], "2026-08-19T03:20:00+00:00")
        # The stored value must survive the pure validator it will be re-checked by.
        self.assertIsNone(validate_schedule(normalized))

    def test_absolute_at_is_left_alone(self):
        normalized, reasons = normalize_schedule(
            {"kind": "at", "at": "2099-01-01T15:00:00+09:00"},
            tz_name="Asia/Tokyo",
            now=self._NOW,
        )

        self.assertEqual(reasons, [])
        self.assertEqual(normalized["at"], "2099-01-01T15:00:00+09:00")

    def test_missing_cron_tz_is_backfilled_from_the_tenant(self):
        normalized, reasons = normalize_schedule(
            {"kind": "cron", "expr": "0 7 * * *"},
            tz_name="Asia/Tokyo",
        )

        self.assertEqual(reasons, ["tz_backfilled"])
        self.assertEqual(normalized["tz"], "Asia/Tokyo")

    def test_blank_cron_tz_is_backfilled(self):
        normalized, reasons = normalize_schedule(
            {"kind": "cron", "expr": "0 7 * * *", "tz": "  "},
            tz_name="Asia/Tokyo",
        )

        self.assertEqual(reasons, ["tz_backfilled"])
        self.assertEqual(normalized["tz"], "Asia/Tokyo")

    def test_explicit_cron_tz_is_preserved(self):
        normalized, reasons = normalize_schedule(
            {"kind": "cron", "expr": "0 7 * * *", "tz": "America/New_York"},
            tz_name="Asia/Tokyo",
        )

        self.assertEqual(reasons, [])
        self.assertEqual(normalized["tz"], "America/New_York")

    def test_normalization_does_not_mutate_the_callers_dict(self):
        submitted = {"kind": "cron", "expr": "0 7 * * *"}
        normalize_schedule(submitted, tz_name="Asia/Tokyo")
        self.assertNotIn("tz", submitted)

    def test_normalization_still_enforces_validation(self):
        with self.assertRaises(ScheduleValidationError) as cm:
            normalize_schedule({"kind": "every", "everyMs": 3600}, tz_name="Asia/Tokyo")
        self.assertEqual(cm.exception.code, "everyms_too_small")

    def test_no_tenant_timezone_falls_back_to_utc(self):
        normalized, reasons = normalize_schedule(
            {"kind": "cron", "expr": "0 7 * * *"},
            tz_name=None,
        )

        self.assertEqual(reasons, ["tz_backfilled"])
        self.assertEqual(normalized["tz"], "UTC")

    def test_unusable_profile_timezone_backfills_utc_rather_than_a_400(self):
        """A profile written before the tz gate must not poison the cron.

        Injecting the bad name would fail validation below and hand the model a
        400 about a field it never sent.
        """
        for bad_tz in ("Etc/GMT+9", "Mars/Olympus"):
            with self.subTest(tz=bad_tz):
                normalized, reasons = normalize_schedule(
                    {"kind": "cron", "expr": "0 7 * * *"},
                    tz_name=bad_tz,
                )
                self.assertEqual(reasons, ["tz_backfilled"])
                self.assertEqual(normalized["tz"], "UTC")
