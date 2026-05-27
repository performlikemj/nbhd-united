"""Tests for billing app."""

from django.test import TestCase

from apps.tenants.services import create_tenant

from .constants import DEEPSEEK_MODEL, MINIMAX_MODEL
from .services import (
    check_budget,
    extract_model_from_response,
    record_usage,
    resolve_model_for_attribution,
    resolve_tenant_primary_model,
)


class UsageTrackingTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Billing Test", telegram_chat_id=444555666)

    def test_record_usage(self):
        record = record_usage(
            tenant=self.tenant,
            event_type="message",
            input_tokens=100,
            output_tokens=200,
            model_used="anthropic/claude-sonnet-4-20250514",
        )
        self.assertEqual(record.input_tokens, 100)
        self.assertEqual(record.output_tokens, 200)
        self.assertGreater(record.cost_estimate, 0)

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.messages_today, 1)
        self.assertEqual(self.tenant.messages_this_month, 1)
        self.assertEqual(self.tenant.tokens_this_month, 300)

    def test_check_budget_within_limits(self):
        self.assertEqual(check_budget(self.tenant), "")

    def test_check_budget_over_limit(self):
        self.tenant.estimated_cost_this_month = self.tenant.effective_cost_budget
        self.tenant.save()
        self.assertEqual(check_budget(self.tenant), "personal")

    def test_check_budget_exempt_skips_personal(self):
        """Exempt tenant is not blocked even when over personal budget."""
        self.tenant.estimated_cost_this_month = self.tenant.effective_cost_budget + 1
        self.tenant.is_budget_exempt = True
        self.tenant.save()
        self.assertEqual(check_budget(self.tenant), "")

    def test_check_budget_exempt_skips_global(self):
        """Exempt tenant is not blocked even when global budget is exhausted."""
        from datetime import date

        from .models import MonthlyBudget

        first = date.today().replace(day=1)
        MonthlyBudget.objects.create(month=first, budget_dollars=100, spent_dollars=200)
        self.tenant.is_budget_exempt = True
        self.tenant.save()
        self.assertEqual(check_budget(self.tenant), "")


class ResolveTenantPrimaryModelTest(TestCase):
    """Coverage for the chain: applied_model → preferred_model → tier default."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Resolve Test", telegram_chat_id=555666777)

    def test_uses_applied_model_first(self):
        self.tenant.applied_model = MINIMAX_MODEL
        self.tenant.preferred_model = DEEPSEEK_MODEL
        self.tenant.save()
        self.assertEqual(resolve_tenant_primary_model(self.tenant), MINIMAX_MODEL)

    def test_falls_back_to_preferred_when_applied_empty(self):
        self.tenant.applied_model = ""
        self.tenant.preferred_model = DEEPSEEK_MODEL
        self.tenant.save()
        self.assertEqual(resolve_tenant_primary_model(self.tenant), DEEPSEEK_MODEL)

    def test_falls_back_to_tier_default_when_both_empty(self):
        # TIER_MODELS["starter"]["primary"] is DeepSeek as of PR #684.
        self.tenant.applied_model = ""
        self.tenant.preferred_model = ""
        self.tenant.save()
        self.assertEqual(resolve_tenant_primary_model(self.tenant), DEEPSEEK_MODEL)


class ResolveModelForAttributionTest(TestCase):
    """Coverage for the response-extract → fallback-to-primary chain."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Attribution Test", telegram_chat_id=666777888)

    def test_prefers_response_extraction_when_present(self):
        # Provider truth in the response wins, even if the tenant primary is different.
        self.tenant.applied_model = DEEPSEEK_MODEL
        self.tenant.save()
        result = {"usage": {"model_used": "openrouter/some/other-model"}}
        self.assertEqual(
            resolve_model_for_attribution(self.tenant, result),
            "openrouter/some/other-model",
        )

    def test_falls_back_to_tenant_primary_when_response_empty(self):
        # OpenClaw 5.7's strict OpenAI-spec response has no upstream model id.
        self.tenant.applied_model = DEEPSEEK_MODEL
        self.tenant.save()
        result = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        self.assertEqual(resolve_model_for_attribution(self.tenant, result), DEEPSEEK_MODEL)

    def test_falls_back_when_only_openclaw_placeholder_present(self):
        # The "openclaw" top-level placeholder is the request-side echo and
        # carries no information; fall back to the tenant primary.
        self.tenant.applied_model = DEEPSEEK_MODEL
        self.tenant.save()
        result = {"model": "openclaw", "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        self.assertEqual(resolve_model_for_attribution(self.tenant, result), DEEPSEEK_MODEL)

    def test_falls_back_when_result_is_not_a_dict(self):
        self.tenant.applied_model = DEEPSEEK_MODEL
        self.tenant.save()
        self.assertEqual(resolve_model_for_attribution(self.tenant, None), DEEPSEEK_MODEL)
        self.assertEqual(resolve_model_for_attribution(self.tenant, "garbage"), DEEPSEEK_MODEL)


class ExtractModelFromResponseTest(TestCase):
    """Direct coverage for the legacy extract helper — preserves the
    pre-PR-#614 invariants in case a future OpenClaw bump starts emitting
    the upstream id again."""

    def test_reads_usage_model_used_first(self):
        result = {"model": "openclaw", "usage": {"model_used": "openrouter/x/y"}}
        self.assertEqual(extract_model_from_response(result), "openrouter/x/y")

    def test_skips_openclaw_placeholder(self):
        result = {"model": "openclaw", "usage": {"prompt_tokens": 10}}
        self.assertEqual(extract_model_from_response(result), "")

    def test_reads_top_level_model_when_real(self):
        result = {"model": "openrouter/x/y", "usage": {"prompt_tokens": 10}}
        self.assertEqual(extract_model_from_response(result), "openrouter/x/y")
