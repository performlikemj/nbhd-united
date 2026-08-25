"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

from datetime import date

from dateutil.relativedelta import relativedelta
from django.test import SimpleTestCase


class DateutilSdkContractTest(SimpleTestCase):
    def test_relativedelta_month_arithmetic_shape(self):
        self.assertEqual(date(2026, 1, 31) + relativedelta(months=1), date(2026, 2, 28))
