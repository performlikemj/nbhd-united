"""Tests for billing app."""

from django.test import TestCase

from apps.tenants.services import create_tenant

from .constants import ANTHROPIC_OPUS_MODEL, ANTHROPIC_SONNET_MODEL, DEEPSEEK_MODEL, MINIMAX_MODEL
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


class BYOBillingTest(TestCase):
    """Coverage for the fix landed in PR #1.7: BYO model usage must NOT
    increment the tenant's $5 cap counter or the platform MonthlyBudget,
    because the tenant pays the provider (Anthropic / future OpenAI Codex)
    directly via their own subscription. We still write the audit row
    (with cost_estimate = 0) and still bump message + token counters."""

    def setUp(self):
        self.tenant = create_tenant(display_name="BYO Billing", telegram_chat_id=555444333)

    def _baseline_cost(self):
        self.tenant.refresh_from_db()
        return self.tenant.estimated_cost_this_month

    def test_byo_sonnet_chat_records_zero_cost(self):
        before = self._baseline_cost()
        record = record_usage(
            tenant=self.tenant,
            event_type="message",
            input_tokens=10000,
            output_tokens=3000,
            model_used=ANTHROPIC_SONNET_MODEL,
        )
        # Audit row is written for visibility.
        self.assertEqual(record.model_used, ANTHROPIC_SONNET_MODEL)
        self.assertEqual(float(record.cost_estimate), 0.0)
        # Tenant counter is unchanged on the dollar side …
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.estimated_cost_this_month, before)
        # … but message + token counters still tick (rate-limit accounting).
        self.assertEqual(self.tenant.messages_today, 1)
        self.assertEqual(self.tenant.tokens_this_month, 13000)

    def test_byo_opus_chat_records_zero_cost(self):
        # Same shape, different BYO model id, including the dotted variant
        # that OpenRouter sometimes echoes back (claude-opus-4.7).
        for model_id in (ANTHROPIC_OPUS_MODEL, ANTHROPIC_OPUS_MODEL.replace("-4-7", "-4.7")):
            with self.subTest(model_id=model_id):
                tenant = create_tenant(
                    display_name=f"BYO opus {model_id}",
                    telegram_chat_id=555000000 + abs(hash(model_id)) % 999,
                )
                record = record_usage(
                    tenant=tenant,
                    event_type="message",
                    input_tokens=5000,
                    output_tokens=2000,
                    model_used=model_id,
                )
                self.assertEqual(float(record.cost_estimate), 0.0)
                tenant.refresh_from_db()
                self.assertEqual(float(tenant.estimated_cost_this_month), 0.0)

    def test_non_byo_chat_still_charges_normally(self):
        # DeepSeek IS in MODEL_RATES — must continue to charge against
        # the tenant cap exactly as before this PR.
        before = self._baseline_cost()
        record = record_usage(
            tenant=self.tenant,
            event_type="message",
            input_tokens=10000,
            output_tokens=3000,
            model_used=DEEPSEEK_MODEL,
        )
        self.assertGreater(record.cost_estimate, 0)
        self.tenant.refresh_from_db()
        self.assertGreater(self.tenant.estimated_cost_this_month, before)

    def test_system_byo_call_still_charges_platform(self):
        # System-side extraction / synthesis hits OR with the shared key
        # even when targeting an anthropic/* model. The platform pays OR,
        # so cost must be non-zero (recorded against MonthlyBudget;
        # the personal counter stays untouched via is_system=True).
        record = record_usage(
            tenant=self.tenant,
            event_type="extraction",
            input_tokens=3000,
            output_tokens=400,
            model_used=ANTHROPIC_SONNET_MODEL,
            is_system=True,
        )
        # Cost is computed (falls to DEFAULT_RATE since anthropic isn't
        # in MODEL_RATES — separate follow-up bug for system-side OR
        # routing, but at least it's not zeroed here).
        self.assertGreater(record.cost_estimate, 0)
        # Personal counter untouched (is_system=True).
        self.tenant.refresh_from_db()
        self.assertEqual(float(self.tenant.estimated_cost_this_month), 0.0)


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
