"""Tests for usage/cost transparency dashboard."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from .constants import MODEL_RATES
from .models import MonthlyBudget, UsageRecord
from .usage_services import get_daily_usage, get_month_boundaries, get_transparency_data, get_usage_summary


class MonthBoundariesTest(TestCase):
    def test_january(self):
        first, last = get_month_boundaries(date(2026, 1, 15))
        self.assertEqual(first, date(2026, 1, 1))
        self.assertEqual(last, date(2026, 1, 31))

    def test_february(self):
        first, last = get_month_boundaries(date(2026, 2, 10))
        self.assertEqual(first, date(2026, 2, 1))
        self.assertEqual(last, date(2026, 2, 28))

    def test_december(self):
        first, last = get_month_boundaries(date(2025, 12, 25))
        self.assertEqual(first, date(2025, 12, 1))
        self.assertEqual(last, date(2025, 12, 31))


class UsageSummaryServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Dashboard Test", telegram_chat_id=999111)
        today = timezone.now()
        # Create usage records for current month
        UsageRecord.objects.create(
            tenant=self.tenant, event_type="message",
            input_tokens=1000, output_tokens=2000,
            model_used="anthropic/claude-sonnet-4-20250514",
            cost_estimate=Decimal("0.033000"),
            created_at=today,
        )
        UsageRecord.objects.create(
            tenant=self.tenant, event_type="message",
            input_tokens=500, output_tokens=1000,
            model_used="anthropic/claude-opus-4-20250514",
            cost_estimate=Decimal("0.027500"),
            created_at=today,
        )
        UsageRecord.objects.create(
            tenant=self.tenant, event_type="tool_call",
            input_tokens=200, output_tokens=100,
            model_used="anthropic/claude-sonnet-4-20250514",
            cost_estimate=Decimal("0.002100"),
            created_at=today,
        )

    def test_summary_totals(self):
        summary = get_usage_summary(self.tenant)
        self.assertEqual(summary["total_input_tokens"], 1700)
        self.assertEqual(summary["total_output_tokens"], 3100)
        self.assertEqual(summary["total_tokens"], 4800)
        self.assertEqual(summary["message_count"], 3)

    def test_summary_by_model(self):
        summary = get_usage_summary(self.tenant)
        models = {m["model"]: m for m in summary["by_model"]}
        self.assertIn("anthropic/claude-sonnet-4-20250514", models)
        self.assertIn("anthropic/claude-opus-4-20250514", models)
        sonnet = models["anthropic/claude-sonnet-4-20250514"]
        self.assertEqual(sonnet["count"], 2)
        self.assertEqual(sonnet["input_tokens"], 1200)

    def test_summary_has_budget(self):
        summary = get_usage_summary(self.tenant)
        self.assertIn("budget", summary)
        self.assertIn("budget_percentage", summary["budget"])

    def test_summary_no_usage(self):
        tenant2 = create_tenant(display_name="Empty", telegram_chat_id=999222)
        summary = get_usage_summary(tenant2)
        self.assertEqual(summary["total_tokens"], 0)
        self.assertEqual(summary["total_cost"], 0.0)
        self.assertEqual(summary["message_count"], 0)
        self.assertEqual(summary["by_model"], [])


class DailyUsageServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Daily Test", telegram_chat_id=999333)
        now = timezone.now()
        yesterday = now - timedelta(days=1)
        # Create 2 today, 1 yesterday (use update to bypass auto_now_add)
        for _ in range(2):
            UsageRecord.objects.create(
                tenant=self.tenant, event_type="message",
                input_tokens=100, output_tokens=200,
                model_used="anthropic/claude-sonnet-4-20250514",
                cost_estimate=Decimal("0.003300"),
            )
        rec = UsageRecord.objects.create(
            tenant=self.tenant, event_type="message",
            input_tokens=100, output_tokens=200,
            model_used="anthropic/claude-sonnet-4-20250514",
            cost_estimate=Decimal("0.003300"),
        )
        UsageRecord.objects.filter(pk=rec.pk).update(created_at=yesterday)

    def test_daily_returns_30_days(self):
        daily = get_daily_usage(self.tenant, days=30)
        self.assertEqual(len(daily), 30)

    def test_daily_fills_zeros(self):
        daily = get_daily_usage(self.tenant, days=30)
        zero_days = [d for d in daily if d["message_count"] == 0]
        self.assertEqual(len(zero_days), 28)

    def test_daily_aggregation(self):
        daily = get_daily_usage(self.tenant, days=30)
        today_str = timezone.now().date().isoformat()
        today_data = next(d for d in daily if d["date"] == today_str)
        self.assertEqual(today_data["message_count"], 2)
        self.assertEqual(today_data["input_tokens"], 200)

    def test_daily_no_usage(self):
        tenant2 = create_tenant(display_name="Empty2", telegram_chat_id=999444)
        daily = get_daily_usage(tenant2, days=7)
        self.assertEqual(len(daily), 7)
        self.assertTrue(all(d["message_count"] == 0 for d in daily))


class TransparencyServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Transparency", telegram_chat_id=999555)
        UsageRecord.objects.create(
            tenant=self.tenant, event_type="message",
            input_tokens=10000, output_tokens=20000,
            model_used="anthropic/claude-sonnet-4-20250514",
            cost_estimate=Decimal("0.330000"),
        )

    def test_transparency_fields(self):
        data = get_transparency_data(self.tenant)
        self.assertEqual(data["subscription_price"], 12.0)
        self.assertIn("your_actual_cost", data)
        self.assertIn("platform_margin", data)
        self.assertIn("margin_percentage", data)
        self.assertIn("model_rates", data)
        self.assertIn("infra_breakdown", data)
        self.assertIn("explanation", data)

    def test_transparency_margin_calc(self):
        data = get_transparency_data(self.tenant)
        self.assertAlmostEqual(
            data["platform_margin"],
            12.0 - data["your_actual_cost"],
            places=2,
        )

    def test_transparency_infra_breakdown(self):
        data = get_transparency_data(self.tenant)
        self.assertEqual(data["infra_breakdown"], {
            "container": 4.00,
            "database_share": 0.5,
            "storage_share": 0.25,
            "total": 4.75,
        })

    def test_transparency_explanation_mentions_infra(self):
        data = get_transparency_data(self.tenant)
        self.assertIn("Infrastructure", data["explanation"])

    def test_transparency_rate_card(self):
        data = get_transparency_data(self.tenant)
        names = [r["display_name"] for r in data["model_rates"]]
        self.assertIn("Claude Opus 4.6", names)
        self.assertIn("Claude Sonnet 4.5", names)
        self.assertIn("Claude Haiku 4.5", names)
        self.assertIn("Kimi K2.5", names)

    def test_transparency_no_usage(self):
        tenant2 = create_tenant(display_name="NoUse", telegram_chat_id=999666)
        data = get_transparency_data(tenant2)
        self.assertEqual(data["your_actual_cost"], 0.0)
        self.assertEqual(data["platform_margin"], 12.0)

    @override_settings(USAGE_DASHBOARD_SUBSCRIPTION_PRICE=12.5)
    def test_transparency_uses_setting_driven_subscription_price(self):
        data = get_transparency_data(self.tenant)
        self.assertEqual(data["subscription_price"], 12.5)


class UsageAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = create_tenant(display_name="API Test", telegram_chat_id=999777)
        UsageRecord.objects.create(
            tenant=self.tenant, event_type="message",
            input_tokens=500, output_tokens=1000,
            model_used="anthropic/claude-sonnet-4-20250514",
            cost_estimate=Decimal("0.016500"),
        )

    def test_summary_unauthenticated(self):
        response = self.client.get("/api/v1/billing/usage/summary/")
        self.assertEqual(response.status_code, 401)

    def test_daily_unauthenticated(self):
        response = self.client.get("/api/v1/billing/usage/daily/")
        self.assertEqual(response.status_code, 401)

    def test_transparency_unauthenticated(self):
        response = self.client.get("/api/v1/billing/usage/transparency/")
        self.assertEqual(response.status_code, 401)

    def test_summary_authenticated(self):
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.get("/api/v1/billing/usage/summary/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("total_tokens", response.data)
        self.assertIn("by_model", response.data)
        self.assertIn("budget", response.data)

    def test_daily_authenticated(self):
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.get("/api/v1/billing/usage/daily/")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 30)

    def test_daily_custom_days(self):
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.get("/api/v1/billing/usage/daily/?days=7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 7)

    def test_daily_invalid_days_returns_400(self):
        self.client.force_authenticate(user=self.tenant.user)
        for invalid in ("abc", "0", "-1", "999"):
            response = self.client.get(f"/api/v1/billing/usage/daily/?days={invalid}")
            self.assertEqual(response.status_code, 400)

    def test_transparency_authenticated(self):
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.get("/api/v1/billing/usage/transparency/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("subscription_price", response.data)
        self.assertIn("model_rates", response.data)
        self.assertIn("explanation", response.data)

    def test_tenant_isolation(self):
        """Ensure one tenant can't see another's usage."""
        other = create_tenant(display_name="Other", telegram_chat_id=999888)
        UsageRecord.objects.create(
            tenant=other, event_type="message",
            input_tokens=9999, output_tokens=9999,
            model_used="anthropic/claude-opus-4-20250514",
            cost_estimate=Decimal("0.500000"),
        )
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.get("/api/v1/billing/usage/summary/")
        self.assertEqual(response.status_code, 200)
        # Should only see our 1500 tokens, not the other tenant's 19998
        self.assertEqual(response.data["total_tokens"], 1500)


class ConstantsTest(TestCase):
    def test_model_rates_structure(self):
        for key, rate in MODEL_RATES.items():
            self.assertIn("input", rate)
            self.assertIn("output", rate)
            self.assertIn("display_name", rate)
            self.assertGreater(rate["input"], 0)
            self.assertGreater(rate["output"], 0)

    def test_output_more_expensive_than_input(self):
        for key, rate in MODEL_RATES.items():
            self.assertGreater(rate["output"], rate["input"])
