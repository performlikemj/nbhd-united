"""Tests for the revenue-percentage donation ledger + manual reconciliation."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.donation_service import (
    _closed_month_first,
    _is_paying_subscriber,
    snapshot_donations_for_month,
)
from apps.billing.models import DonationLedger
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

JUNE = date(2026, 6, 1)

# Where compute_donation resolves the subscription price. Patch here to simulate
# either the real Stripe price or the settings fallback without touching Stripe.
PRICE_PATH = "apps.billing.usage_services._get_subscription_price"


@override_settings(DONATION_REVENUE_PCT=10.0)
class DonationLedgerTest(TestCase):
    def _paying_tenant(self, chat_id, *, donation_enabled=True):
        t = create_tenant(display_name=f"Donor{chat_id}", telegram_chat_id=chat_id)
        t.status = Tenant.Status.ACTIVE
        t.stripe_subscription_id = f"sub_{chat_id}"
        t.is_trial = False
        t.donation_enabled = donation_enabled
        t.save()
        return t

    @patch(PRICE_PATH, return_value=12.0)
    def test_paying_subscriber_gets_pending_row(self, _price):
        t = self._paying_tenant(811001)
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 1)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        # donation = $12 subscription price * 10% pledge = $1.20
        self.assertEqual(row.donation_amount, Decimal("1.2000"))
        # surplus_amount reused as the revenue base the donation was computed from
        self.assertEqual(row.surplus_amount, Decimal("12.0000"))
        self.assertEqual(row.donation_percentage, 10)

    @patch(PRICE_PATH, return_value=12.0)
    def test_non_paying_no_row(self, _price):
        t = self._paying_tenant(811002)
        t.stripe_subscription_id = ""  # never subscribed
        t.save(update_fields=["stripe_subscription_id"])
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 0)
        self.assertFalse(DonationLedger.objects.filter(tenant=t, month=JUNE).exists())

    @patch(PRICE_PATH, return_value=12.0)
    def test_trialing_skipped_no_row(self, _price):
        t = self._paying_tenant(811003)
        t.is_trial = True
        t.trial_ends_at = timezone.now() + timedelta(days=5)
        t.save(update_fields=["is_trial", "trial_ends_at"])
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(DonationLedger.objects.filter(tenant=t, month=JUNE).exists())

    @patch(PRICE_PATH, return_value=0.05)
    def test_below_threshold_writes_skipped_status(self, _price):
        # $0.05 * 10% = $0.005 < $0.01 threshold → recorded but SKIPPED.
        t = self._paying_tenant(811004)
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 0)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.status, DonationLedger.Status.SKIPPED)
        self.assertEqual(row.donation_amount, Decimal("0.0050"))

    @override_settings(DONATION_REVENUE_PCT=25.0)
    @patch(PRICE_PATH, return_value=12.0)
    def test_pct_setting_override(self, _price):
        t = self._paying_tenant(811005)
        snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        # $12 * 25% = $3.00
        self.assertEqual(row.donation_amount, Decimal("3.0000"))
        self.assertEqual(row.donation_percentage, 25)

    @patch(PRICE_PATH, return_value=20.0)
    def test_donation_tracks_resolved_price(self, _price):
        # Whatever _get_subscription_price resolves (real Stripe price OR the
        # settings fallback), the donation is that price * pledge %.
        t = self._paying_tenant(811006)
        snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("20.0000"))
        self.assertEqual(row.donation_amount, Decimal("2.0000"))  # 20 * 10%

    @patch(PRICE_PATH, return_value=12.0)
    def test_donation_enabled_false_still_donates(self, _price):
        # The revenue pledge is a platform commitment — the per-tenant toggle does
        # NOT gate it. A paying subscriber who opted their surplus out still counts.
        t = self._paying_tenant(811007, donation_enabled=False)
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 1)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.donation_amount, Decimal("1.2000"))

    @patch(PRICE_PATH, return_value=12.0)
    def test_idempotent_and_completed_not_rewritten(self, _price):
        t = self._paying_tenant(811008)
        snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        row.status = DonationLedger.Status.COMPLETED
        row.receipt_reference = "receipt-1"
        row.save()
        # re-run: must NOT rewrite a completed (already-disbursed) row
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["already_completed"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, DonationLedger.Status.COMPLETED)
        self.assertEqual(row.receipt_reference, "receipt-1")
        self.assertEqual(DonationLedger.objects.filter(tenant=t, month=JUNE).count(), 1)

    def test_closed_month_first_wraps_january(self):
        self.assertEqual(_closed_month_first(date(2026, 1, 15)), date(2025, 12, 1))
        self.assertEqual(_closed_month_first(date(2026, 7, 2)), date(2026, 6, 1))

    def test_is_paying_subscriber(self):
        t = self._paying_tenant(811009)
        self.assertTrue(_is_paying_subscriber(t))
        t.stripe_subscription_id = ""
        self.assertFalse(_is_paying_subscriber(t))


class ReconcileDonationsCommandTest(TestCase):
    def _pending_row(self, chat_id, amount):
        t = create_tenant(display_name=f"R{chat_id}", telegram_chat_id=chat_id)
        return DonationLedger.objects.create(
            tenant=t,
            month=JUNE,
            surplus_amount=Decimal(amount),
            donation_amount=Decimal(amount),
            donation_percentage=10,
            status=DonationLedger.Status.PENDING,
        )

    def test_complete_marks_pending_rows(self):
        r1 = self._pending_row(822001, "5.00")
        r2 = self._pending_row(822002, "3.00")
        call_command("reconcile_donations", "--month", "2026-06", "--complete", "--receipt", "bank #123")
        for r in (r1, r2):
            r.refresh_from_db()
            self.assertEqual(r.status, DonationLedger.Status.COMPLETED)
            self.assertEqual(r.receipt_reference, "bank #123")

    def test_complete_requires_receipt(self):
        with self.assertRaises(CommandError):
            call_command("reconcile_donations", "--month", "2026-06", "--complete")

    def test_list_mode_leaves_rows_pending_and_shows_dollars_only(self):
        r = self._pending_row(822003, "5.00")
        out = StringIO()
        call_command("reconcile_donations", "--month", "2026-06", stdout=out)
        r.refresh_from_db()
        self.assertEqual(r.status, DonationLedger.Status.PENDING)
        output = out.getvalue()
        self.assertIn("TOTAL to disburse: $5.00", output)
        # No charity/meal language — no disbursement agreement exists yet.
        self.assertNotIn("meal", output.lower())
