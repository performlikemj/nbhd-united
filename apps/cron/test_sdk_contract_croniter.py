"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

from datetime import UTC, datetime

from croniter import croniter
from django.test import SimpleTestCase


class CroniterSdkContractTest(SimpleTestCase):
    def test_get_next_datetime_shape(self):
        schedule = croniter("*/5 * * * *", datetime(2026, 8, 24, tzinfo=UTC))

        self.assertEqual(schedule.get_next(datetime), datetime(2026, 8, 24, 0, 5, tzinfo=UTC))
