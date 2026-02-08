"""Tests for billing app."""
import os
from django.test import TestCase

from apps.tenants.services import create_tenant
from .models import UsageRecord
from .services import check_budget, record_usage


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
        self.assertTrue(check_budget(self.tenant))

    def test_check_budget_over_limit(self):
        self.tenant.tokens_this_month = self.tenant.monthly_token_budget
        self.tenant.save()
        self.assertFalse(check_budget(self.tenant))
