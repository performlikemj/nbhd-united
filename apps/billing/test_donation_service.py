"""Tests for the revenue-percentage donation ledger + manual reconciliation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
from apps.billing.models import CreditLedger, DonationLedger
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

JUNE = date(2026, 6, 1)
MAY = date(2026, 5, 1)

# Timestamps for exercising the cash-basis month window: IN_JUNE lands inside
# the [2026-06-01, 2026-07-01) window computed by donation_service._month_window;
# PRIOR_MONTH and NEXT_MONTH land just outside it on either side.
IN_JUNE = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
PRIOR_MONTH = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
NEXT_MONTH = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

# Where compute_donation resolves the subscription price (+ its source). Patched
# here — where the name is defined and re-imported at call time — to simulate a
# Stripe-verified price or the settings fallback without touching Stripe. The
# mock returns a (price, source) tuple; source "stripe" books a PENDING pledge,
# "fallback" is booked SKIPPED.
PRICE_PATH = "apps.billing.usage_services._get_subscription_price_with_source"


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

    @patch(PRICE_PATH, return_value=(12.0, "stripe"))
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

    @patch(PRICE_PATH, return_value=(12.0, "stripe"))
    def test_non_paying_no_row(self, _price):
        t = self._paying_tenant(811002)
        t.stripe_subscription_id = ""  # never subscribed
        t.save(update_fields=["stripe_subscription_id"])
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 0)
        self.assertFalse(DonationLedger.objects.filter(tenant=t, month=JUNE).exists())

    @patch(PRICE_PATH, return_value=(12.0, "stripe"))
    def test_trialing_skipped_no_row(self, _price):
        t = self._paying_tenant(811003)
        t.is_trial = True
        t.trial_ends_at = timezone.now() + timedelta(days=5)
        t.save(update_fields=["is_trial", "trial_ends_at"])
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(DonationLedger.objects.filter(tenant=t, month=JUNE).exists())

    @patch(PRICE_PATH, return_value=(0.05, "stripe"))
    def test_below_threshold_writes_skipped_status(self, _price):
        # $0.05 * 10% = $0.005 < $0.01 threshold → recorded but SKIPPED.
        t = self._paying_tenant(811004)
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 0)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.status, DonationLedger.Status.SKIPPED)
        self.assertEqual(row.donation_amount, Decimal("0.0050"))

    @override_settings(DONATION_REVENUE_PCT=25.0)
    @patch(PRICE_PATH, return_value=(12.0, "stripe"))
    def test_pct_setting_override(self, _price):
        t = self._paying_tenant(811005)
        snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        # $12 * 25% = $3.00
        self.assertEqual(row.donation_amount, Decimal("3.0000"))
        self.assertEqual(row.donation_percentage, 25)

    @patch(PRICE_PATH, return_value=(20.0, "stripe"))
    def test_donation_tracks_resolved_price(self, _price):
        # A Stripe-verified price of any amount books a PENDING pledge of
        # price * pledge %.
        t = self._paying_tenant(811006)
        snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("20.0000"))
        self.assertEqual(row.donation_amount, Decimal("2.0000"))  # 20 * 10%

    @patch(PRICE_PATH, return_value=(12.0, "stripe"))
    def test_donation_enabled_false_still_donates(self, _price):
        # The revenue pledge is a platform commitment — the per-tenant toggle does
        # NOT gate it. A paying subscriber who opted their surplus out still counts.
        t = self._paying_tenant(811007, donation_enabled=False)
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 1)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.donation_amount, Decimal("1.2000"))

    @patch(PRICE_PATH, return_value=(12.0, "stripe"))
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

    @patch(PRICE_PATH, return_value=(12.0, "fallback"))
    def test_unverified_price_writes_skipped_not_pending(self, _price):
        # A fallback (non-Stripe-verified) price must NOT book a pending pledge —
        # we won't promise money against revenue we couldn't verify was collected.
        # A warning is logged so this is loud in production.
        t = self._paying_tenant(811010)
        with self.assertLogs("apps.billing.donation_service", level="WARNING") as logs:
            result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["pending_total"], 0.0)
        self.assertEqual(result["skipped"], 1)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.status, DonationLedger.Status.SKIPPED)
        # Revenue base is still recorded for auditability, but no pledge is made.
        self.assertEqual(row.surplus_amount, Decimal("12.0000"))
        self.assertTrue(any("unverified subscription price" in m for m in logs.output))

    def test_unverified_upgrades_to_pending_on_rerun(self):
        # First snapshot: Stripe unavailable → the price falls back → SKIPPED, no pledge.
        t = self._paying_tenant(811011)
        with patch(PRICE_PATH, return_value=(12.0, "fallback")):
            snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.status, DonationLedger.Status.SKIPPED)
        # Re-run once Stripe recovers: the same row upgrades to PENDING (update_or_create
        # is idempotent — one row, now with the real verified price).
        with patch(PRICE_PATH, return_value=(12.0, "stripe")):
            result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["pending"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.donation_amount, Decimal("1.2000"))
        self.assertEqual(DonationLedger.objects.filter(tenant=t, month=JUNE).count(), 1)

    def test_closed_month_first_wraps_january(self):
        self.assertEqual(_closed_month_first(date(2026, 1, 15)), date(2025, 12, 1))
        self.assertEqual(_closed_month_first(date(2026, 7, 2)), date(2026, 6, 1))

    def test_is_paying_subscriber(self):
        t = self._paying_tenant(811009)
        self.assertTrue(_is_paying_subscriber(t))
        t.stripe_subscription_id = ""
        self.assertFalse(_is_paying_subscriber(t))


@override_settings(DONATION_REVENUE_PCT=10.0)
class TopupDonationRevenueTest(TestCase):
    """Credit top-up revenue folded into the monthly donation base: grant
    valuation (amount_paid_cents / pack fallback / unresolvable-skip), refund
    netting in revenue units (full / partial / unmatched), the cash-basis month
    boundary, the negative floor, and the three candidate shapes (trial /
    credit-only / verified-or-unverified subscriber)."""

    def _credit_tenant(self, chat_id, *, is_trial=False, trial_ends_at=None, exempt=False):
        # No stripe_subscription_id — a candidate only via top-up ledger activity.
        t = create_tenant(display_name=f"Credit{chat_id}", telegram_chat_id=chat_id)
        t.status = Tenant.Status.ACTIVE
        t.is_trial = is_trial
        t.trial_ends_at = trial_ends_at
        t.is_budget_exempt = exempt
        t.save()
        return t

    def _subscriber(self, chat_id):
        t = create_tenant(display_name=f"Sub{chat_id}", telegram_chat_id=chat_id)
        t.status = Tenant.Status.ACTIVE
        t.stripe_subscription_id = f"sub_{chat_id}"
        t.is_trial = False
        t.save()
        return t

    def _grant(self, tenant, *, event_id, amount, amount_paid_cents=None, pack_id="", pi="", when=None):
        row = CreditLedger.objects.create(
            tenant=tenant,
            kind=CreditLedger.Kind.GRANT,
            amount=Decimal(amount),
            stripe_event_id=event_id,
            stripe_payment_intent_id=pi,
            pack_id=pack_id,
            amount_paid_cents=amount_paid_cents,
            description="Top-up",
        )
        if when is not None:
            CreditLedger.objects.filter(pk=row.pk).update(created_at=when)
        return row

    def _reversal(self, tenant, *, event_id, amount, pi="", when=None):
        # `amount` is negative — reversals are stored as negative credit-dollar clawbacks.
        row = CreditLedger.objects.create(
            tenant=tenant,
            kind=CreditLedger.Kind.REVERSAL,
            amount=Decimal(amount),
            stripe_event_id=event_id,
            stripe_payment_intent_id=pi,
            description="Refund clawback",
        )
        if when is not None:
            CreditLedger.objects.filter(pk=row.pk).update(created_at=when)
        return row

    def test_grant_counted_by_amount_paid_only_inside_month_window(self):
        # Identical grants in the prior and next month must NOT bleed into June.
        t = self._credit_tenant(831001)
        self._grant(t, event_id="evt_831001_in", amount="5.00", amount_paid_cents=600, pack_id="credit_5", when=IN_JUNE)
        self._grant(
            t, event_id="evt_831001_prior", amount="5.00", amount_paid_cents=600, pack_id="credit_5", when=PRIOR_MONTH
        )
        self._grant(
            t, event_id="evt_831001_next", amount="5.00", amount_paid_cents=600, pack_id="credit_5", when=NEXT_MONTH
        )
        result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.surplus_amount, Decimal("6.0000"))  # only the in-window $6.00 grant
        self.assertEqual(row.donation_amount, Decimal("0.6000"))
        self.assertEqual(result["pending"], 1)

    def test_grant_null_amount_paid_falls_back_to_pack_price(self):
        t = self._credit_tenant(831002)
        self._grant(t, event_id="evt_831002", amount="5.00", amount_paid_cents=None, pack_id="credit_5", when=IN_JUNE)
        with self.assertLogs("apps.billing.donation_service", level="WARNING") as logs:
            result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("6.0000"))  # credit_5 pack price_cents=600
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.donation_amount, Decimal("0.6000"))
        self.assertEqual(result["pending"], 1)
        self.assertTrue(any("missing amount_paid_cents" in m for m in logs.output))

    def test_grant_unresolvable_skipped_contributes_zero(self):
        t = self._credit_tenant(831003)
        self._grant(
            t, event_id="evt_831003", amount="5.00", amount_paid_cents=None, pack_id="unknown_pack", when=IN_JUNE
        )
        with self.assertLogs("apps.billing.donation_service", level="WARNING") as logs:
            result = snapshot_donations_for_month(JUNE)
        self.assertFalse(DonationLedger.objects.filter(tenant=t, month=JUNE).exists())
        self.assertEqual(result["pending"], 0)
        self.assertTrue(any("neither amount_paid_cents nor a resolvable pack_id" in m for m in logs.output))

    def test_full_refund_nets_to_zero_revenue(self):
        # Grant: $6.00 revenue / $5.00 credit. Full reversal of the $5.00 credit
        # must claw back the full $6.00 revenue (frac=1), not just $5.00.
        t = self._credit_tenant(831004)
        self._grant(
            t,
            event_id="evt_831004_g",
            amount="5.00",
            amount_paid_cents=600,
            pack_id="credit_5",
            pi="pi_831004",
            when=IN_JUNE,
        )
        self._reversal(t, event_id="evt_831004_r", amount="-5.00", pi="pi_831004", when=IN_JUNE)
        result = snapshot_donations_for_month(JUNE)
        self.assertFalse(DonationLedger.objects.filter(tenant=t, month=JUNE).exists())
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_partial_refund_nets_proportionally(self):
        # frac = 2.50/5.00 = 0.5 → net = 6.00 - 0.5*6.00 = 3.00.
        t = self._credit_tenant(831005)
        self._grant(
            t,
            event_id="evt_831005_g",
            amount="5.00",
            amount_paid_cents=600,
            pack_id="credit_5",
            pi="pi_831005",
            when=IN_JUNE,
        )
        self._reversal(t, event_id="evt_831005_r", amount="-2.50", pi="pi_831005", when=IN_JUNE)
        result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("3.0000"))
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.donation_amount, Decimal("0.3000"))
        self.assertEqual(result["pending"], 1)

    def test_cash_basis_refund_lands_in_refund_month_not_grant_month(self):
        # Grant lands in May, its full refund lands in June.
        t = self._credit_tenant(831006)
        self._grant(
            t,
            event_id="evt_831006_g",
            amount="5.00",
            amount_paid_cents=600,
            pack_id="credit_5",
            pi="pi_831006",
            when=PRIOR_MONTH,
        )
        self._reversal(t, event_id="evt_831006_r", amount="-5.00", pi="pi_831006", when=IN_JUNE)

        # June: no June grants, the refund of a May grant would go negative
        # (0 - 6.00) — floored at 0, with a floor warning, no row for a
        # non-subscriber with zero net revenue.
        with self.assertLogs("apps.billing.donation_service", level="WARNING") as logs:
            result_june = snapshot_donations_for_month(JUNE)
        self.assertFalse(DonationLedger.objects.filter(tenant=t, month=JUNE).exists())
        self.assertEqual(result_june["pending"], 0)
        self.assertTrue(any("went negative" in m for m in logs.output))

        # May: the grant is still counted in full — the June-dated refund is
        # outside May's window and doesn't touch May's snapshot at all.
        result_may = snapshot_donations_for_month(MAY)
        row_may = DonationLedger.objects.get(tenant=t, month=MAY)
        self.assertEqual(row_may.surplus_amount, Decimal("6.0000"))
        self.assertEqual(row_may.status, DonationLedger.Status.PENDING)
        self.assertEqual(result_may["pending"], 1)

    def test_unmatched_reversal_subtracts_raw_credit_amount(self):
        # Reversal's PI matches no grant → falls back to netting the raw
        # credit-dollar clawback directly (under-subtracts by the pack margin).
        t = self._credit_tenant(831007)
        self._grant(
            t, event_id="evt_831007_g", amount="10.00", amount_paid_cents=1100, pack_id="credit_10", when=IN_JUNE
        )
        self._reversal(t, event_id="evt_831007_r", amount="-3.00", pi="pi_no_match", when=IN_JUNE)
        with self.assertLogs("apps.billing.donation_service", level="WARNING") as logs:
            result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("8.0000"))  # $11.00 grant - raw $3.00 clawback
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(result["pending"], 1)
        self.assertTrue(any("no resolvable matching grant revenue" in m for m in logs.output))

    def test_trial_tenant_with_topup_gets_row_base_is_topup_only(self):
        t = self._credit_tenant(831008, is_trial=True, trial_ends_at=timezone.now() + timedelta(days=5))
        self._grant(t, event_id="evt_831008", amount="5.00", amount_paid_cents=600, pack_id="credit_5", when=IN_JUNE)
        result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("6.0000"))
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.donation_amount, Decimal("0.6000"))
        self.assertEqual(result["pending"], 1)

    def test_credit_only_tenant_is_a_candidate_and_gets_a_row(self):
        t = self._credit_tenant(831009)  # no sub id, not trial
        self.assertEqual(t.stripe_subscription_id, "")
        self._grant(t, event_id="evt_831009", amount="10.00", amount_paid_cents=1100, pack_id="credit_10", when=IN_JUNE)
        result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("11.0000"))
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(result["pending"], 1)

    @patch(PRICE_PATH, return_value=(12.0, "stripe"))
    def test_verified_subscriber_with_topups_combines_both_components(self, _price):
        t = self._subscriber(831010)
        self._grant(t, event_id="evt_831010", amount="5.00", amount_paid_cents=600, pack_id="credit_5", when=IN_JUNE)
        result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("18.0000"))  # $12 sub + $6 topup
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.donation_amount, Decimal("1.8000"))
        self.assertEqual(result["pending"], 1)

    def test_unverified_sub_price_with_topups_counts_topups_only_then_upgrades(self):
        t = self._subscriber(831011)
        self._grant(t, event_id="evt_831011", amount="5.00", amount_paid_cents=600, pack_id="credit_5", when=IN_JUNE)

        with (
            patch(PRICE_PATH, return_value=(12.0, "fallback")),
            self.assertLogs("apps.billing.donation_service", level="WARNING") as logs,
        ):
            result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("6.0000"))  # sub component excluded, topups only
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.donation_amount, Decimal("0.6000"))
        self.assertEqual(result["pending"], 1)
        self.assertTrue(any("unverified subscription price" in m for m in logs.output))

        # Stripe recovers: re-run upgrades the SAME row to include the sub component.
        with patch(PRICE_PATH, return_value=(12.0, "stripe")):
            result2 = snapshot_donations_for_month(JUNE)
        row.refresh_from_db()
        self.assertEqual(row.surplus_amount, Decimal("18.0000"))  # $12 sub + $6 topup
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.donation_amount, Decimal("1.8000"))
        self.assertEqual(result2["pending"], 1)
        self.assertEqual(DonationLedger.objects.filter(tenant=t, month=JUNE).count(), 1)

    def test_budget_exempt_tenant_with_topups_gets_no_row(self):
        t = self._credit_tenant(831012, exempt=True)
        self._grant(t, event_id="evt_831012", amount="5.00", amount_paid_cents=600, pack_id="credit_5", when=IN_JUNE)
        result = snapshot_donations_for_month(JUNE)
        self.assertFalse(DonationLedger.objects.filter(tenant=t, month=JUNE).exists())
        self.assertEqual(result["pending"], 0)

    def test_completed_row_not_rewritten_when_topups_added_later(self):
        t = self._credit_tenant(831013)
        self._grant(t, event_id="evt_831013_g1", amount="5.00", amount_paid_cents=600, pack_id="credit_5", when=IN_JUNE)
        snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        row.status = DonationLedger.Status.COMPLETED
        row.receipt_reference = "receipt-topup-1"
        row.save()

        # More top-up revenue appears for the same month after disbursement.
        self._grant(
            t, event_id="evt_831013_g2", amount="10.00", amount_paid_cents=1100, pack_id="credit_10", when=IN_JUNE
        )
        result = snapshot_donations_for_month(JUNE)
        self.assertEqual(result["already_completed"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, DonationLedger.Status.COMPLETED)
        self.assertEqual(row.surplus_amount, Decimal("6.0000"))  # unchanged, not rewritten to 17.00
        self.assertEqual(row.receipt_reference, "receipt-topup-1")
        self.assertEqual(DonationLedger.objects.filter(tenant=t, month=JUNE).count(), 1)

    def test_below_threshold_topup_only_written_skipped(self):
        # $0.05 * 10% = $0.005 < $0.01 threshold → recorded but SKIPPED.
        t = self._credit_tenant(831014)
        self._grant(t, event_id="evt_831014", amount="0.05", amount_paid_cents=5, when=IN_JUNE)
        result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("0.0500"))
        self.assertEqual(row.status, DonationLedger.Status.SKIPPED)
        self.assertEqual(row.donation_amount, Decimal("0.0050"))
        self.assertEqual(result["pending"], 0)

    def test_discounted_charge_books_amount_paid_not_list_price(self):
        # A promo/coupon charged $5.00 for the credit_5 pack (list price
        # $6.00) — revenue must book what was actually collected, never the
        # pack's list price. This is the "never book MORE than collected"
        # direction of the truth rule.
        t = self._credit_tenant(831015)
        self._grant(t, event_id="evt_831015", amount="5.00", amount_paid_cents=500, pack_id="credit_5", when=IN_JUNE)
        result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("5.0000"))  # NOT 6.0000 (list price)
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.donation_amount, Decimal("0.5000"))
        self.assertEqual(result["pending"], 1)

    def test_double_partial_refunds_net_to_exactly_zero(self):
        # Two $2.50 partial refunds against the same $6.00-revenue/$5.00-credit
        # grant, same PI: 2 * (2.50/5.00)*6.00 = $6.00 clawed back == the
        # grant's full revenue — nets to exactly 0, not -0.50 or a rounding drift.
        t = self._credit_tenant(831016)
        self._grant(
            t,
            event_id="evt_831016_g",
            amount="5.00",
            amount_paid_cents=600,
            pack_id="credit_5",
            pi="pi_831016",
            when=IN_JUNE,
        )
        self._reversal(t, event_id="evt_831016_r1", amount="-2.50", pi="pi_831016", when=IN_JUNE)
        self._reversal(t, event_id="evt_831016_r2", amount="-2.50", pi="pi_831016", when=IN_JUNE)
        result = snapshot_donations_for_month(JUNE)
        self.assertFalse(DonationLedger.objects.filter(tenant=t, month=JUNE).exists())
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_matched_unresolvable_grant_reversal_pair_nets_zero_symmetrically(self):
        # F1: a reversal matched to a grant whose revenue is unresolvable must
        # be SKIPPED (the pair nets $0 on both sides), never subtracted raw —
        # subtracting raw would take money away from a DIFFERENT grant's
        # revenue in the same month (the pre-fix bug).
        t = self._credit_tenant(831017)
        self._grant(
            t,
            event_id="evt_831017_g_bad",
            amount="5.00",
            amount_paid_cents=None,
            pack_id="retired_pack",
            pi="pi_831017_bad",
            when=IN_JUNE,
        )
        self._reversal(t, event_id="evt_831017_r_bad", amount="-5.00", pi="pi_831017_bad", when=IN_JUNE)
        self._grant(
            t, event_id="evt_831017_g_clean", amount="10.00", amount_paid_cents=1100, pack_id="credit_10", when=IN_JUNE
        )
        with self.assertLogs("apps.billing.donation_service", level="WARNING") as logs:
            result = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        # $11.00 from the clean grant only. The old bug would have subtracted
        # the unmatched-fallback raw clawback ($5.00) from this total, giving
        # $6.00 instead of the correct $11.00.
        self.assertEqual(row.surplus_amount, Decimal("11.0000"))
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(result["pending"], 1)
        self.assertTrue(any("skipping this reversal so the pair nets" in m for m in logs.output))

    def test_no_downward_pending_rewrite_after_verification_regression(self):
        t = self._subscriber(831018)
        self._grant(t, event_id="evt_831018", amount="5.00", amount_paid_cents=600, pack_id="credit_5", when=IN_JUNE)

        with patch(PRICE_PATH, return_value=(12.0, "stripe")):
            result1 = snapshot_donations_for_month(JUNE)
        row = DonationLedger.objects.get(tenant=t, month=JUNE)
        self.assertEqual(row.surplus_amount, Decimal("18.0000"))  # $12 sub + $6 topup
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(row.donation_amount, Decimal("1.8000"))
        self.assertEqual(result1["pending"], 1)

        # Stripe becomes unavailable: the sub component would drop to $0, so the
        # recomputed base ($6.00 topup-only) is LOWER than the existing verified
        # PENDING row ($18.00) — must NOT rewrite downward.
        with (
            patch(PRICE_PATH, return_value=(12.0, "fallback")),
            self.assertLogs("apps.billing.donation_service", level="WARNING") as logs,
        ):
            result2 = snapshot_donations_for_month(JUNE)
        row.refresh_from_db()
        self.assertEqual(row.surplus_amount, Decimal("18.0000"))  # untouched
        self.assertEqual(row.donation_amount, Decimal("1.8000"))
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(result2["pending"], 1)  # still counted pending in the summary
        self.assertTrue(any("is LOWER than the existing PENDING row" in m for m in logs.output))

        # Stripe recovers again: an equal recompute is a normal no-op rewrite.
        with patch(PRICE_PATH, return_value=(12.0, "stripe")):
            result3 = snapshot_donations_for_month(JUNE)
        row.refresh_from_db()
        self.assertEqual(row.surplus_amount, Decimal("18.0000"))
        self.assertEqual(row.status, DonationLedger.Status.PENDING)
        self.assertEqual(result3["pending"], 1)


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
