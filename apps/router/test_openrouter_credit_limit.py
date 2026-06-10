"""Tests for the OpenRouter credit-limit detection + hibernate path
(PR #1.6 Phase 4). When OR returns a 402 (or any error whose body
contains a credit-limit needle), pending_queue's drain functions must
trip the budget circuit breaker rather than retry the message."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.router.pending_queue import (
    _handle_openrouter_credit_limit,
    _looks_like_openrouter_credit_limit,
)
from apps.tenants.services import create_tenant


def _resp(status: int, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


class LooksLikeOpenRouterCreditLimitTest(TestCase):
    def test_402_is_credit_limit(self):
        self.assertTrue(_looks_like_openrouter_credit_limit(_resp(402, "")))
        self.assertTrue(_looks_like_openrouter_credit_limit(_resp(402, "any body")))

    def test_429_with_credit_text_matches(self):
        self.assertTrue(_looks_like_openrouter_credit_limit(_resp(429, '{"error":{"message":"Credit limit reached"}}')))

    def test_4xx_with_quota_text_matches(self):
        self.assertTrue(
            _looks_like_openrouter_credit_limit(_resp(403, '{"error":{"message":"insufficient credit balance"}}'))
        )

    def test_200_with_unrelated_error_does_not_match(self):
        self.assertFalse(_looks_like_openrouter_credit_limit(_resp(200, '{"error":{"message":"model not found"}}')))

    def test_500_with_generic_internal_error_does_not_match(self):
        # OpenClaw 5.7 wraps everything as "internal error" — by design
        # we don't fire on those (the hourly reconcile cron catches the
        # cap hit within ~1h instead).
        self.assertFalse(
            _looks_like_openrouter_credit_limit(_resp(500, '{"error":{"message":"internal error","type":"api_error"}}'))
        )

    def test_4xx_unrelated_does_not_match(self):
        self.assertFalse(
            _looks_like_openrouter_credit_limit(_resp(400, '{"error":{"message":"invalid_request_error"}}'))
        )

    def test_empty_body_4xx_does_not_match_without_402(self):
        self.assertFalse(_looks_like_openrouter_credit_limit(_resp(503, "")))


class HandleOpenRouterCreditLimitTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="OR Limit Test", telegram_chat_id=888001)
        self.tenant.estimated_cost_this_month = Decimal("2.50")
        self.tenant.save()

    @patch("apps.router.line_webhook._send_line_text")
    @patch("apps.router.views._hibernate_for_quota")
    def test_line_path_bumps_cap_hibernates_and_messages(self, mock_hibernate, mock_send):
        _handle_openrouter_credit_limit(self.tenant, channel="line", channel_user_id="U123")

        self.tenant.refresh_from_db()
        # Bumped to effective_cost_budget so check_budget fires next time.
        self.assertEqual(
            self.tenant.estimated_cost_this_month,
            Decimal(str(self.tenant.effective_cost_budget)),
        )
        mock_hibernate.assert_called_once()
        mock_send.assert_called_once()
        call_args = mock_send.call_args.args
        self.assertEqual(call_args[0], "U123")
        # Message text is non-empty (real content depends on translation).
        self.assertTrue(call_args[1])

    @patch("apps.router.pending_queue._send_telegram_markdown")
    @patch("apps.router.views._hibernate_for_quota")
    def test_telegram_path_bumps_cap_hibernates_and_messages(self, mock_hibernate, mock_send):
        _handle_openrouter_credit_limit(self.tenant, channel="telegram", channel_user_id="123456")

        self.tenant.refresh_from_db()
        self.assertEqual(
            self.tenant.estimated_cost_this_month,
            Decimal(str(self.tenant.effective_cost_budget)),
        )
        mock_hibernate.assert_called_once()
        mock_send.assert_called_once()
        # chat_id arg was coerced to int.
        self.assertEqual(mock_send.call_args.args[0], 123456)

    @patch("apps.billing.credits.sync_or_key_limit")
    @patch("apps.router.views._hibernate_for_quota")
    def test_budget_exempt_raises_ceiling_not_hibernated(self, mock_hibernate, mock_sync):
        """A budget-exempt tenant (canary/internal) that 402s must have its OR key
        ceiling raised and NOT be hibernated — the 2026-06-10 canary outage."""
        self.tenant.is_budget_exempt = True
        self.tenant.save(update_fields=["is_budget_exempt"])

        _handle_openrouter_credit_limit(self.tenant, channel="telegram", channel_user_id="123456")

        mock_sync.assert_called_once()
        mock_hibernate.assert_not_called()

    @patch("apps.router.pending_queue._send_telegram_markdown")
    @patch("apps.router.views._hibernate_for_quota")
    def test_telegram_invalid_chat_id_skips_send_without_crashing(self, mock_hibernate, mock_send):
        _handle_openrouter_credit_limit(self.tenant, channel="telegram", channel_user_id="not-an-int")
        # Hibernation + cap update still happen even when the message
        # can't be delivered.
        self.tenant.refresh_from_db()
        self.assertEqual(
            self.tenant.estimated_cost_this_month,
            Decimal(str(self.tenant.effective_cost_budget)),
        )
        mock_hibernate.assert_called_once()
        mock_send.assert_not_called()
