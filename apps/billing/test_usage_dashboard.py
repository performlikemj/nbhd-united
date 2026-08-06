"""Tests for usage/cost transparency dashboard."""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant

from .constants import (
    ANTHROPIC_SONNET_MODEL,
    DEEPSEEK_FLASH_DISPLAY,
    DEEPSEEK_MODEL,
    GEMMA_RATE,
    MODEL_RATES,
)
from .models import UsageRecord
from .usage_services import (
    _get_subscription_price,
    get_daily_usage,
    get_month_boundaries,
    get_transparency_data,
    get_usage_summary,
)


def _stripe_subscription(unit_amount, currency="usd", interval="month", interval_count=1):
    """Build a plain-dict Stripe Subscription (as ``.to_dict()`` would return)."""
    return {
        "id": "sub_test",
        "items": {
            "data": [
                {
                    "price": {
                        "unit_amount": unit_amount,
                        "currency": currency,
                        "recurring": {"interval": interval, "interval_count": interval_count},
                    }
                }
            ]
        },
    }


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
            tenant=self.tenant,
            event_type="message",
            input_tokens=1000,
            output_tokens=2000,
            model_used="anthropic/claude-sonnet-4-20250514",
            cost_estimate=Decimal("0.033000"),
            created_at=today,
        )
        UsageRecord.objects.create(
            tenant=self.tenant,
            event_type="message",
            input_tokens=500,
            output_tokens=1000,
            model_used="anthropic/claude-opus-4-20250514",
            cost_estimate=Decimal("0.027500"),
            created_at=today,
        )
        UsageRecord.objects.create(
            tenant=self.tenant,
            event_type="tool_call",
            input_tokens=200,
            output_tokens=100,
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
        self.assertIn("tenant_cost_used", summary["budget"])
        self.assertIn("tenant_cost_budget", summary["budget"])
        self.assertGreater(summary["budget"]["tenant_cost_budget"], 0)

    def test_summary_no_usage(self):
        tenant2 = create_tenant(display_name="Empty", telegram_chat_id=999222)
        summary = get_usage_summary(tenant2)
        self.assertEqual(summary["total_tokens"], 0)
        self.assertEqual(summary["total_cost"], 0.0)
        self.assertEqual(summary["message_count"], 0)
        self.assertEqual(summary["by_model"], [])


class UsageSummaryReconciliationTest(TestCase):
    """The totals card and budget bar must agree, per-model spellings must
    collapse, and metered rows must scale to the reconciled provider total
    while BYO rows stay $0."""

    def _mk(self, model_used, cost, *, tokens=1000):
        UsageRecord.objects.create(
            tenant=self.tenant,
            event_type="message",
            input_tokens=tokens,
            output_tokens=tokens,
            model_used=model_used,
            cost_estimate=Decimal(str(cost)),
        )

    def _set_reconciled(self, amount):
        self.tenant.estimated_cost_this_month = Decimal(str(amount))
        self.tenant.save(update_fields=["estimated_cost_this_month"])

    def setUp(self):
        self.tenant = create_tenant(display_name="Reconcile", telegram_chat_id=990001)

    def test_two_spellings_collapse_and_scale_to_reconciled_total(self):
        # Same model, two spellings (the screenshot bug) + one other metered model.
        self._mk("anthropic/claude-haiku-4-5", "0.002")
        self._mk("anthropic/claude-haiku-4.5", "0.001")
        self._mk(DEEPSEEK_MODEL, "0.003")
        self._set_reconciled("0.60")

        summary = get_usage_summary(self.tenant)

        # Headline total == authoritative reconciled spend == budget bar.
        self.assertAlmostEqual(summary["total_cost"], 0.60, places=6)
        self.assertAlmostEqual(summary["budget"]["tenant_cost_used"], 0.60, places=6)

        by_model = summary["by_model"]
        # Haiku's two spellings collapsed into a single row → 2 rows total.
        self.assertEqual(len(by_model), 2)
        haiku = next(m for m in by_model if m["model"] == "anthropic/claude-haiku-4-5")
        self.assertEqual(haiku["count"], 2)
        self.assertEqual(haiku["billing"], "metered")

        # Metered rows scaled proportionally so they sum to the reconciled total.
        self.assertAlmostEqual(sum(m["cost"] for m in by_model), 0.60, places=6)

    def test_byo_rows_pinned_zero_and_synthetic_other_carries_remainder(self):
        # Only a BYO (subscription) row exists, but the tenant has real reconciled
        # metered spend — the remainder must land on a synthetic "other" row.
        self._mk(ANTHROPIC_SONNET_MODEL, "0")
        self._set_reconciled("0.25")

        summary = get_usage_summary(self.tenant)
        self.assertAlmostEqual(summary["total_cost"], 0.25, places=6)

        by_model = {m["model"]: m for m in summary["by_model"]}
        sonnet = by_model[ANTHROPIC_SONNET_MODEL]
        self.assertEqual(sonnet["cost"], 0.0)
        self.assertEqual(sonnet["billing"], "subscription")

        other = by_model["other"]
        self.assertAlmostEqual(other["cost"], 0.25, places=6)
        self.assertEqual(other["display_name"], "Other usage")
        self.assertEqual(other["billing"], "metered")
        self.assertAlmostEqual(sum(m["cost"] for m in summary["by_model"]), 0.25, places=6)

    def test_zero_reconciled_total_leaves_raw_estimates(self):
        # Fresh tenant (no reconciliation yet) → estimated_cost_this_month == 0.
        # Rows keep their raw estimate rather than being scaled to zero.
        self._mk("anthropic/claude-haiku-4-5", "0.002")

        summary = get_usage_summary(self.tenant)
        self.assertEqual(summary["total_cost"], 0.0)
        haiku = next(m for m in summary["by_model"] if m["model"] == "anthropic/claude-haiku-4-5")
        self.assertAlmostEqual(haiku["cost"], 0.002, places=6)


class UsageSummaryTrueCostTest(TestCase):
    """The additive 'true monthly cost' fields (llm_cost / infra_cost /
    infra_source / true_total_cost / infra_breakdown) on the usage summary.

    Covers BOTH the azure-snapshot path and the DoesNotExist→estimate fallback —
    CI historically only exercised the estimate branch, so both are pinned here.
    The quota-driving budget block is asserted untouched.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="TrueCost", telegram_chat_id=991200)
        self.tenant.estimated_cost_this_month = Decimal("1.50")
        self.tenant.save(update_fields=["estimated_cost_this_month"])
        self.month = get_month_boundaries()[0]

    def test_estimate_path_true_cost_fields(self):
        # No snapshot → estimate fallback. Fresh tenant (not active / no
        # container) → db share capped $0.50, flat platform estimate $2.00.
        summary = get_usage_summary(self.tenant)
        self.assertEqual(summary["llm_cost"], 1.50)
        self.assertEqual(summary["infra_source"], "estimate")
        self.assertEqual(summary["infra_cost"], 6.75)  # 4.00 + 0.25 + 0.50 + 2.00
        self.assertAlmostEqual(summary["true_total_cost"], 1.50 + 6.75, places=4)
        self.assertEqual(
            summary["infra_breakdown"],
            {
                "container": 4.00,
                "database_share": 0.5,
                "storage_share": 0.25,
                "platform_share": 2.00,
            },
        )
        # Budget block (quota) is untouched by the true-cost fields.
        self.assertIn("tenant_cost_budget", summary["budget"])

    def test_azure_snapshot_path_true_cost_fields(self):
        from .models import InfraCostSnapshot

        InfraCostSnapshot.objects.create(
            tenant=self.tenant,
            month=self.month,
            container_cost=Decimal("3.50"),
            storage_cost=Decimal("0.10"),
            database_share=Decimal("0.25"),
            platform_share=Decimal("12.0000"),
            total_cost=Decimal("15.85"),
            source="azure",
        )
        summary = get_usage_summary(self.tenant)
        self.assertEqual(summary["infra_source"], "azure")
        self.assertAlmostEqual(summary["infra_cost"], 15.85, places=4)
        self.assertEqual(summary["llm_cost"], 1.50)
        self.assertAlmostEqual(summary["true_total_cost"], 1.50 + 15.85, places=4)
        self.assertEqual(summary["infra_breakdown"]["platform_share"], 12.0)
        self.assertEqual(summary["infra_breakdown"]["container"], 3.50)

    def test_true_total_is_llm_plus_infra(self):
        summary = get_usage_summary(self.tenant)
        self.assertAlmostEqual(
            summary["true_total_cost"],
            summary["llm_cost"] + summary["infra_cost"],
            places=4,
        )


class DailyUsageServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Daily Test", telegram_chat_id=999333)
        now = timezone.now()
        yesterday = now - timedelta(days=1)
        # Create 2 today, 1 yesterday (use update to bypass auto_now_add)
        for _ in range(2):
            UsageRecord.objects.create(
                tenant=self.tenant,
                event_type="message",
                input_tokens=100,
                output_tokens=200,
                model_used="anthropic/claude-sonnet-4-20250514",
                cost_estimate=Decimal("0.003300"),
            )
        rec = UsageRecord.objects.create(
            tenant=self.tenant,
            event_type="message",
            input_tokens=100,
            output_tokens=200,
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
            tenant=self.tenant,
            event_type="message",
            input_tokens=10000,
            output_tokens=20000,
            model_used="anthropic/claude-sonnet-4-20250514",
            cost_estimate=Decimal("0.330000"),
        )

    def test_transparency_fields(self):
        data = get_transparency_data(self.tenant)
        self.assertEqual(data["subscription_price"], 12.0)
        self.assertIn("your_actual_cost", data)
        self.assertIn("platform_infra", data)
        self.assertIn("surplus", data)
        self.assertIn("donation_amount", data)
        self.assertIn("donation_enabled", data)
        self.assertIn("donation_percentage", data)
        self.assertIn("model_rates", data)
        self.assertIn("infra_breakdown", data)
        self.assertIn("explanation", data)

    def test_transparency_infra_value(self):
        data = get_transparency_data(self.tenant)
        # container 4.00 + db 0.50 + storage 0.25 + platform 2.00 (estimate).
        self.assertEqual(data["platform_infra"], 6.75)

    def test_transparency_infra_breakdown(self):
        data = get_transparency_data(self.tenant)
        self.assertEqual(
            data["infra_breakdown"],
            {
                "container": 4.00,
                "database_share": 0.5,
                "storage_share": 0.25,
                "platform_share": 2.00,
                "total": 6.75,
                "source": "estimate",
            },
        )

    def test_transparency_explanation_mentions_infra(self):
        data = get_transparency_data(self.tenant)
        self.assertIn("infrastructure", data["explanation"].lower())

    def test_transparency_rate_card(self):
        # Default tenant is starter tier — rate card filtered to tier models
        data = get_transparency_data(self.tenant)
        names = [r["display_name"] for r in data["model_rates"]]
        self.assertIn(DEEPSEEK_FLASH_DISPLAY, names)

    def test_transparency_no_usage(self):
        tenant2 = create_tenant(display_name="NoUse", telegram_chat_id=999666)
        data = get_transparency_data(tenant2)
        self.assertEqual(data["your_actual_cost"], 0.0)
        self.assertEqual(data["platform_infra"], 6.75)

    def test_surplus_calculation(self):
        data = get_transparency_data(self.tenant)
        expected_surplus = max(0, 12.0 - data["your_actual_cost"] - data["platform_infra"])
        self.assertAlmostEqual(data["surplus"], expected_surplus, places=4)

    def test_transparency_explanation_mentions_platform_share(self):
        data = get_transparency_data(self.tenant)
        self.assertIn("platform share", data["explanation"].lower())

    def test_donation_zero_for_non_paying_subscriber(self):
        # Revenue-% model: donation comes from collected subscription revenue, so a
        # tenant with no Stripe subscription contributes $0 — regardless of the
        # (now-vestigial) per-tenant donation toggle/percentage.
        self.tenant.stripe_subscription_id = ""
        self.tenant.donation_enabled = True
        self.tenant.donation_percentage = 50
        self.tenant.save(update_fields=["stripe_subscription_id", "donation_enabled", "donation_percentage"])
        data = get_transparency_data(self.tenant)
        self.assertEqual(data["donation_amount"], 0.0)
        # The toggle fields are still returned unchanged for the settings UI.
        self.assertTrue(data["donation_enabled"])
        self.assertEqual(data["donation_percentage"], 50)

    @override_settings(DONATION_REVENUE_PCT=10.0)
    @mock.patch("apps.billing.usage_services._get_subscription_price", return_value=12.0)
    def test_donation_is_revenue_pct_for_paying_subscriber(self, _price):
        # Paying subscriber → donation is a flat % of the subscription price,
        # independent of usage/surplus and NOT gated by the donation toggle.
        self.tenant.stripe_subscription_id = "sub_test"
        self.tenant.is_trial = False
        self.tenant.donation_enabled = False
        self.tenant.save(update_fields=["stripe_subscription_id", "is_trial", "donation_enabled"])
        data = get_transparency_data(self.tenant)
        # $12 subscription price * 10% pledge = $1.20, even with the toggle off.
        self.assertAlmostEqual(data["donation_amount"], 1.20, places=4)


class DonationPreferenceAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = create_tenant(display_name="Donation Test", telegram_chat_id=999900)

    def test_unauthenticated(self):
        response = self.client.patch("/api/v1/billing/donation-preference/")
        self.assertEqual(response.status_code, 401)

    def test_toggle_donation_on(self):
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.patch(
            "/api/v1/billing/donation-preference/",
            {"donation_enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["donation_enabled"])
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.donation_enabled)

    def test_set_percentage(self):
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.patch(
            "/api/v1/billing/donation-preference/",
            {"donation_percentage": 75},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["donation_percentage"], 75)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.donation_percentage, 75)

    def test_invalid_percentage(self):
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.patch(
            "/api/v1/billing/donation-preference/",
            {"donation_percentage": 150},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class UsageAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = create_tenant(display_name="API Test", telegram_chat_id=999777)
        UsageRecord.objects.create(
            tenant=self.tenant,
            event_type="message",
            input_tokens=500,
            output_tokens=1000,
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
        # True-cost fields survive serialization (UsageSummarySerializer).
        self.assertIn("llm_cost", response.data)
        self.assertIn("infra_cost", response.data)
        self.assertIn("infra_source", response.data)
        self.assertIn("true_total_cost", response.data)
        self.assertIn("platform_share", response.data["infra_breakdown"])

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
        self.assertIn("surplus", response.data)
        self.assertIn("donation_amount", response.data)
        self.assertIn("donation_enabled", response.data)
        self.assertIn("donation_percentage", response.data)
        self.assertIn("model_rates", response.data)
        self.assertIn("explanation", response.data)

    def test_tenant_isolation(self):
        """Ensure one tenant can't see another's usage."""
        other = create_tenant(display_name="Other", telegram_chat_id=999888)
        UsageRecord.objects.create(
            tenant=other,
            event_type="message",
            input_tokens=9999,
            output_tokens=9999,
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
            # Free promo models (e.g. Nemotron 3 Ultra) legitimately price at 0;
            # metered models must be positive.
            self.assertGreaterEqual(rate["input"], 0)
            self.assertGreaterEqual(rate["output"], 0)

    def test_output_more_expensive_than_input(self):
        for key, rate in MODEL_RATES.items():
            # The output>input invariant only applies to metered models — free
            # models price both at 0.
            if rate["input"] == 0 and rate["output"] == 0:
                continue
            self.assertGreater(rate["output"], rate["input"])

    def test_gemma_rate_matches_openrouter_live_pricing(self):
        """Pinned to OpenRouter's published price (verified 2026-08-06).

        Gemma is the pdfModel pin and the default compose model, so it carries
        real per-tenant spend. A drift here silently over- or under-bills every
        future turn, and nothing else in the system reconciles it.
        """
        self.assertEqual(GEMMA_RATE, {"input": 0.10, "output": 0.34})


@override_settings(
    USAGE_DASHBOARD_SUBSCRIPTION_PRICE=12.0,
    STRIPE_LIVE_MODE=False,
    STRIPE_TEST_SECRET_KEY="sk_test_dummy",
)
class SubscriptionPriceTest(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = create_tenant(display_name="SubPrice", telegram_chat_id=998001)

    def tearDown(self):
        cache.clear()

    def _link_subscription(self, sub_id="sub_live_123"):
        self.tenant.stripe_subscription_id = sub_id
        self.tenant.save(update_fields=["stripe_subscription_id"])

    def test_no_subscription_uses_fallback(self):
        # Fresh tenants have no stripe_subscription_id — parity with the old flat $12.
        self.assertEqual(self.tenant.stripe_subscription_id, "")
        with mock.patch("stripe.Subscription.retrieve") as retrieve:
            self.assertEqual(_get_subscription_price(self.tenant), 12.0)
        retrieve.assert_not_called()

    def test_reads_monthly_price_from_stripe(self):
        self._link_subscription()
        with mock.patch(
            "stripe.Subscription.retrieve",
            return_value=_stripe_subscription(2500),
        ) as retrieve:
            price = _get_subscription_price(self.tenant)
        self.assertEqual(price, 25.0)
        retrieve.assert_called_once()

    def test_yearly_price_normalized_to_monthly(self):
        self._link_subscription()
        with mock.patch(
            "stripe.Subscription.retrieve",
            return_value=_stripe_subscription(12000, interval="year"),
        ):
            price = _get_subscription_price(self.tenant)
        self.assertEqual(price, 10.0)

    def test_non_usd_falls_back(self):
        self._link_subscription()
        with mock.patch(
            "stripe.Subscription.retrieve",
            return_value=_stripe_subscription(2500, currency="eur"),
        ):
            self.assertEqual(_get_subscription_price(self.tenant), 12.0)

    def test_unsupported_interval_falls_back(self):
        self._link_subscription()
        with mock.patch(
            "stripe.Subscription.retrieve",
            return_value=_stripe_subscription(500, interval="week"),
        ):
            self.assertEqual(_get_subscription_price(self.tenant), 12.0)

    def test_api_error_falls_back(self):
        self._link_subscription()
        with mock.patch(
            "stripe.Subscription.retrieve",
            side_effect=Exception("boom"),
        ):
            self.assertEqual(_get_subscription_price(self.tenant), 12.0)

    def test_failure_is_negative_cached(self):
        # A Stripe outage must not make every render pay a slow failing call:
        # the first failure caches the fallback, the second serves it without Stripe.
        self._link_subscription()
        with mock.patch(
            "stripe.Subscription.retrieve",
            side_effect=Exception("stripe down"),
        ) as retrieve:
            first = _get_subscription_price(self.tenant)
            second = _get_subscription_price(self.tenant)
        self.assertEqual(first, 12.0)
        self.assertEqual(second, 12.0)
        retrieve.assert_called_once()

    def test_cache_hit_avoids_second_stripe_call(self):
        self._link_subscription()
        with mock.patch(
            "stripe.Subscription.retrieve",
            return_value=_stripe_subscription(2500),
        ) as retrieve:
            first = _get_subscription_price(self.tenant)
            second = _get_subscription_price(self.tenant)
        self.assertEqual(first, 25.0)
        self.assertEqual(second, 25.0)
        retrieve.assert_called_once()

    def test_handles_stripe_object_with_to_dict(self):
        # stripe-py returns a StripeObject, not a plain dict — we coerce via to_dict().
        self._link_subscription()
        stripe_obj = mock.Mock()
        stripe_obj.to_dict.return_value = _stripe_subscription(2500)
        with mock.patch("stripe.Subscription.retrieve", return_value=stripe_obj):
            price = _get_subscription_price(self.tenant)
        self.assertEqual(price, 25.0)

    @override_settings(USAGE_DASHBOARD_SUBSCRIPTION_PRICE=15.0)
    def test_fallback_honors_setting_override(self):
        self.assertEqual(_get_subscription_price(self.tenant), 15.0)

    def test_missing_api_key_falls_back(self):
        self._link_subscription()
        with override_settings(STRIPE_TEST_SECRET_KEY=""):
            with mock.patch("stripe.Subscription.retrieve") as retrieve:
                self.assertEqual(_get_subscription_price(self.tenant), 12.0)
            retrieve.assert_not_called()
