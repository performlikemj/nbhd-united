"""Tests for the per-tenant cost-cap email handlers (PR #1.8)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import TestCase

from apps.router.billing_quota_handlers import (
    _format_dollars,
    _format_reset_date,
    fire_threshold_emails_if_crossed,
    send_cost_approaching_email,
    send_cost_exhausted_email,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class FormattersTest(TestCase):
    def test_format_dollars_quantizes_to_2dp(self):
        self.assertEqual(_format_dollars(Decimal("4.5135")), "$4.51")
        self.assertEqual(_format_dollars(5), "$5.00")
        self.assertEqual(_format_dollars("0.7"), "$0.70")

    def test_format_reset_date_month_boundary(self):
        from datetime import date

        # Mid-month → next month's 1st
        self.assertEqual(_format_reset_date(date(2026, 5, 20)), "June 1, 2026")
        # December → January next year
        self.assertEqual(_format_reset_date(date(2026, 12, 5)), "January 1, 2027")


class ApproachingEmailTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Approach Test", telegram_chat_id=111000111)
        self.tenant.user.email = "user@example.com"
        self.tenant.user.save()
        self.tenant.estimated_cost_this_month = Decimal("4.50")  # 90% of $5
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save()
        mail.outbox = []

    def test_happy_path_sends_email_and_sets_marker(self):
        sent = send_cost_approaching_email(self.tenant)
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["user@example.com"])
        # HTML alternative attached
        alternatives = [(content, mimetype) for content, mimetype in (msg.alternatives or [])]
        html_parts = [c for c, mt in alternatives if mt == "text/html"]
        self.assertEqual(len(html_parts), 1)
        # Subject contains the percentage
        self.assertIn("90%", msg.subject)
        # Marker set
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.cost_warn_sent_at)

    def test_idempotent_on_re_call(self):
        self.assertTrue(send_cost_approaching_email(self.tenant))
        mail.outbox = []
        # Second call: short-circuits on the marker
        self.tenant.refresh_from_db()
        self.assertFalse(send_cost_approaching_email(self.tenant))
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_when_budget_exempt(self):
        self.tenant.is_budget_exempt = True
        self.tenant.save()
        self.assertFalse(send_cost_approaching_email(self.tenant))
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_when_no_email_address(self):
        self.tenant.user.email = ""
        self.tenant.user.save()
        self.assertFalse(send_cost_approaching_email(self.tenant))
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_when_tenant_not_active(self):
        self.tenant.status = Tenant.Status.DELETED
        self.tenant.save()
        self.assertFalse(send_cost_approaching_email(self.tenant))
        self.assertEqual(len(mail.outbox), 0)


class ExhaustedEmailTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Exhaust Test", telegram_chat_id=222000222)
        self.tenant.user.email = "user@example.com"
        self.tenant.user.save()
        self.tenant.estimated_cost_this_month = Decimal("5.20")  # over cap
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save()
        mail.outbox = []

    def test_happy_path_sends_email_and_sets_marker(self):
        sent = send_cost_exhausted_email(self.tenant)
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["user@example.com"])
        # Reset date appears in subject
        self.assertIn(", 20", msg.subject)  # year fragment "20XX"
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.cost_exhausted_email_sent_at)

    def test_idempotent(self):
        send_cost_exhausted_email(self.tenant)
        mail.outbox = []
        self.tenant.refresh_from_db()
        self.assertFalse(send_cost_exhausted_email(self.tenant))
        self.assertEqual(len(mail.outbox), 0)

    def test_budget_exempt_skipped(self):
        self.tenant.is_budget_exempt = True
        self.tenant.save()
        self.assertFalse(send_cost_exhausted_email(self.tenant))


class FireThresholdEmailsTest(TestCase):
    """Coverage for the shared threshold-crossing detector used by the
    reconcile cron + the 402 detector."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Threshold Test", telegram_chat_id=333000333)
        self.tenant.user.email = "user@example.com"
        self.tenant.user.save()
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save()
        mail.outbox = []

    def test_crossing_90_pct_fires_approaching_only(self):
        # Before $4 (80%), after $4.60 (92%) → crosses 90% threshold
        out = fire_threshold_emails_if_crossed(
            self.tenant,
            before=Decimal("4.00"),
            after=Decimal("4.60"),
        )
        self.assertTrue(out["warn"])
        self.assertFalse(out["exhausted"])
        self.assertEqual(len(mail.outbox), 1)

    def test_crossing_full_cap_fires_both(self):
        # Before $4 (80%), after $5.10 (>100%) crosses BOTH thresholds in
        # one update. Both emails fire.
        out = fire_threshold_emails_if_crossed(
            self.tenant,
            before=Decimal("4.00"),
            after=Decimal("5.10"),
        )
        self.assertTrue(out["warn"])
        self.assertTrue(out["exhausted"])
        self.assertEqual(len(mail.outbox), 2)

    def test_no_crossing_no_emails(self):
        out = fire_threshold_emails_if_crossed(
            self.tenant,
            before=Decimal("4.00"),
            after=Decimal("4.40"),  # both below 90%
        )
        self.assertFalse(out["warn"])
        self.assertFalse(out["exhausted"])
        self.assertEqual(len(mail.outbox), 0)

    def test_already_above_threshold_does_not_re_fire(self):
        # before is already past 90%, after grows further → no NEW crossing
        out = fire_threshold_emails_if_crossed(
            self.tenant,
            before=Decimal("4.60"),  # already > 90%
            after=Decimal("4.80"),
        )
        self.assertFalse(out["warn"])
        self.assertFalse(out["exhausted"])

    def test_zero_cap_safely_no_ops(self):
        # Pathological: effective_cost_budget = 0 (shouldn't happen in
        # practice but the helper must not div-by-zero).
        with patch.object(Tenant, "effective_cost_budget", Decimal("0")):
            out = fire_threshold_emails_if_crossed(
                self.tenant,
                before=Decimal("0"),
                after=Decimal("100"),
            )
        self.assertFalse(out["warn"])
        self.assertFalse(out["exhausted"])


class MonthlyResetClearsMarkersTest(TestCase):
    def test_reset_clears_quota_email_markers(self):
        from django.utils import timezone

        from apps.tenants.services import reset_monthly_counters

        t = create_tenant(display_name="Reset Test", telegram_chat_id=444000444)
        t.cost_warn_sent_at = timezone.now()
        t.cost_exhausted_email_sent_at = timezone.now()
        t.messages_this_month = 50  # so the filter clause matches
        t.save()

        reset_monthly_counters()

        t.refresh_from_db()
        self.assertIsNone(t.cost_warn_sent_at)
        self.assertIsNone(t.cost_exhausted_email_sent_at)
