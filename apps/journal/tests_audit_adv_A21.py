"""Adversarial-audit regression tests for cluster A21 (FA-0742).

Guards the ?since= parameter validation in SessionListView against
out-of-range date strings that raise ValueError from Django's
parse_date/parse_datetime instead of returning None.
"""

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.services import create_tenant

from .session_models import Session


class SessionListSinceParamTest(TestCase):
    """FA-0742: ?since= must return 400 for ALL invalid inputs, not just
    syntactically-malformed strings.  Out-of-range dates like 2021-13-45
    raise ValueError from parse_date/parse_datetime; the fix wraps the
    call in try/except so those also resolve to a clean 400.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Since Test User", telegram_chat_id=9001)
        self.user = self.tenant.user
        refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        Session.objects.create(
            tenant=self.tenant,
            source="yardtalk-mac/1.0.0",
            project="proj-a",
            session_start="2026-01-15T10:00:00Z",
            session_end="2026-01-15T11:00:00Z",
            summary="Session in January",
        )
        Session.objects.create(
            tenant=self.tenant,
            source="yardtalk-mac/1.0.0",
            project="proj-a",
            session_start="2026-06-01T10:00:00Z",
            session_end="2026-06-01T11:00:00Z",
            summary="Session in June",
        )

    def _get(self, since):
        return self.client.get(f"/api/v1/sessions/?since={since}")

    # --- out-of-range cases (previously raised ValueError → HTTP 500) ---

    def test_since_out_of_range_month_returns_400(self):
        """Month 13 is out of range; must be a 400, not a 500."""
        response = self._get("2021-13-45")
        self.assertEqual(response.status_code, 400)
        self.assertIn("since", response.json()["detail"].lower())

    def test_since_out_of_range_day_feb_returns_400(self):
        """Feb 30 does not exist; must be a 400, not a 500."""
        response = self._get("2021-02-30")
        self.assertEqual(response.status_code, 400)
        self.assertIn("since", response.json()["detail"].lower())

    def test_since_out_of_range_datetime_raises_400(self):
        """Datetime with invalid month must be a 400, not a 500."""
        response = self._get("2021-13-45T10:00:00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("since", response.json()["detail"].lower())

    def test_since_out_of_range_hour_returns_400(self):
        """Hour 25 is out of range; must be a 400, not a 500."""
        response = self._get("2021-01-01T25:61:00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("since", response.json()["detail"].lower())

    # --- syntactically-malformed case (handled before the fix, still works) ---

    def test_since_not_a_date_returns_400(self):
        """Completely non-date string must still return 400."""
        response = self._get("not-a-date")
        self.assertEqual(response.status_code, 400)
        self.assertIn("since", response.json()["detail"].lower())

    # --- valid cases must still work ---

    def test_since_valid_date_filters_correctly(self):
        """A valid ISO date should filter sessions on or after that date."""
        response = self._get("2026-03-01")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["summary"], "Session in June")

    def test_since_valid_datetime_filters_correctly(self):
        """A valid ISO datetime should filter sessions on or after that moment."""
        response = self._get("2026-01-16T00:00:00Z")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["summary"], "Session in June")

    def test_since_before_all_sessions_returns_all(self):
        """A since value before all sessions should return all non-test sessions."""
        response = self._get("2020-01-01")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 2)
