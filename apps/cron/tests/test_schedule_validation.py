"""Table-driven tests for schedule intake validation."""

from django.test import SimpleTestCase

from apps.cron.schedule_validation import (
    ScheduleValidationError,
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
