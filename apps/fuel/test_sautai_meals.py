"""Consumer endpoint tests for the linked Sautai meal surface."""

from datetime import UTC, date, datetime, timedelta
from time import monotonic
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.integrations.models import Integration, SautaiMealPlanJob, SautaiMealPlanJobStatus
from apps.tenants.services import create_tenant


class FuelMealsTodayViewTests(TestCase):
    url = "/api/v1/fuel/meals/today/"

    def setUp(self):
        cache.clear()
        self.tenant = create_tenant(display_name="Meal Surface", telegram_chat_id=801101)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def tearDown(self):
        cache.clear()

    def _link(self, tenant=None, sautai_user_id=501):
        return Integration.objects.create(
            tenant=tenant or self.tenant,
            provider=Integration.Provider.SAUTAI,
            status=Integration.Status.ACTIVE,
            sautai_user_id=sautai_user_id,
            linked_at=timezone.now(),
        )

    @staticmethod
    def _plan_for(day, meals):
        week_start = day - timedelta(days=day.weekday())
        return {
            "week_start": week_start.isoformat(),
            "week_end": (week_start + timedelta(days=6)).isoformat(),
            "days": [{"day": day.strftime("%A"), "meals": meals}],
        }

    def test_requires_authentication(self):
        self.client.credentials()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    @patch("apps.integrations.sautai_client.fetch_sautai_current_plan")
    @patch("apps.fuel.views.tenant_today", return_value=date(2026, 7, 22))
    def test_linked_plan_returns_todays_meals_only(self, mock_today, mock_fetch):
        self._link()
        mock_fetch.return_value = {
            "outcome": "ok",
            "plan": self._plan_for(
                mock_today.return_value,
                [
                    {"meal_type": "Breakfast", "name": "Rice and eggs", "calories": 520},
                    {
                        "meal_type": "Dinner",
                        "name": "Miso-glazed salmon",
                        "note": "Light before tomorrow's intervals",
                        "macros": {"protein": 40},
                    },
                ],
            ),
        }

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "meals": [
                    {"slot": "breakfast", "name": "Rice and eggs", "note": "", "date": "2026-07-22"},
                    {
                        "slot": "dinner",
                        "name": "Miso-glazed salmon",
                        "note": "Light before tomorrow's intervals",
                        "date": "2026-07-22",
                    },
                ]
            },
        )
        mock_fetch.assert_called_once_with(
            identity={"sautai_user_id": 501},
            week_start_iso="2026-07-20",
            timeout_seconds=3.0,
        )

    @patch("apps.integrations.sautai_client.fetch_sautai_current_plan")
    def test_unlinked_tenant_returns_empty_without_calling_sautai(self, mock_fetch):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"meals": []})
        mock_fetch.assert_not_called()

    @patch("apps.integrations.sautai_client.fetch_sautai_current_plan", return_value={"outcome": "not_found"})
    def test_linked_tenant_with_no_plan_returns_empty(self, mock_fetch):
        self._link()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"meals": []})
        mock_fetch.assert_called_once()

    @patch(
        "apps.integrations.sautai_client.fetch_sautai_current_plan",
        return_value={"outcome": "error", "detail": "sautai_error_500"},
    )
    def test_sautai_5xx_degrades_to_empty_200(self, mock_fetch):
        self._link()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"meals": []})
        mock_fetch.assert_called_once()

    @patch(
        "apps.integrations.sautai_client.fetch_sautai_current_plan",
        return_value={"outcome": "error", "detail": "request_failed: timeout"},
    )
    def test_sautai_timeout_degrades_quickly_with_bounded_timeout(self, mock_fetch):
        self._link()
        started = monotonic()
        response = self.client.get(self.url)
        elapsed = monotonic() - started
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"meals": []})
        self.assertLess(elapsed, 1.0)
        self.assertEqual(mock_fetch.call_args.kwargs["timeout_seconds"], 3.0)

    @patch("apps.integrations.sautai_client.fetch_sautai_current_plan")
    def test_jst_2330_uses_tenant_local_today(self, mock_fetch):
        self._link()
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        jst_2330_as_utc = datetime(2026, 7, 22, 14, 30, tzinfo=UTC)
        local_day = date(2026, 7, 22)
        mock_fetch.return_value = {
            "outcome": "ok",
            "plan": self._plan_for(local_day, [{"meal_type": "Dinner", "name": "Cold soba"}]),
        }

        with patch("django.utils.timezone.now", return_value=jst_2330_as_utc):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meals"][0]["date"], "2026-07-22")
        self.assertEqual(mock_fetch.call_args.kwargs["week_start_iso"], "2026-07-20")

    @patch("apps.integrations.sautai_client.fetch_sautai_current_plan")
    @patch("apps.fuel.views.tenant_today", return_value=date(2026, 7, 22))
    def test_second_request_uses_per_tenant_cache(self, mock_today, mock_fetch):
        self._link()
        mock_fetch.return_value = {
            "outcome": "ok",
            "plan": self._plan_for(
                mock_today.return_value,
                [{"meal_type": "Dinner", "name": "Cached curry"}],
            ),
        }

        first = self.client.get(self.url)
        second = self.client.get(self.url)

        self.assertEqual(first["X-Cache"], "MISS")
        self.assertEqual(second["X-Cache"], "HIT")
        self.assertEqual(first.json(), second.json())
        mock_fetch.assert_called_once()

    @patch(
        "apps.integrations.sautai_client.fetch_sautai_current_plan",
        side_effect=RuntimeError("Miso-glazed salmon is private user content"),
    )
    def test_partner_exception_logs_no_meal_content(self, mock_fetch):
        self._link()
        with self.assertLogs("apps.fuel.views", level="WARNING") as captured:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"meals": []})
        self.assertNotIn("Miso-glazed salmon", "\n".join(captured.output))
        mock_fetch.assert_called_once()

    @patch("apps.integrations.sautai_client.fetch_sautai_current_plan")
    @patch("apps.fuel.views.tenant_today", return_value=date(2026, 7, 22))
    def test_one_truth_reads_live_client_not_local_job_table(self, mock_today, mock_fetch):
        self._link()
        SautaiMealPlanJob.objects.create(
            tenant=self.tenant,
            week_start="2026-07-20",
            status=SautaiMealPlanJobStatus.READY,
            result=self._plan_for(
                mock_today.return_value,
                [{"meal_type": "Dinner", "name": "Stale local-table dinner"}],
            ),
        )
        mock_fetch.return_value = {
            "outcome": "ok",
            "plan": self._plan_for(
                mock_today.return_value,
                [{"meal_type": "Dinner", "name": "Live Sautai dinner"}],
            ),
        }

        response = self.client.get(self.url)

        self.assertEqual(response.json()["meals"][0]["name"], "Live Sautai dinner")
        mock_fetch.assert_called_once()

    @patch("apps.integrations.sautai_client.fetch_sautai_current_plan")
    def test_link_from_another_tenant_cannot_authorize_the_request(self, mock_fetch):
        other = create_tenant(display_name="Other Meal Surface", telegram_chat_id=801102)
        self._link(tenant=other, sautai_user_id=999)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"meals": []})
        mock_fetch.assert_not_called()
