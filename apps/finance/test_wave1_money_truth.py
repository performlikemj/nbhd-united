"""Wave 1 money-truth fixes — the failing inputs that used to succeed.

Every test here starts from a call the finance tools accepted and answered
confidently with the wrong number in it: a deposit that destroyed the money, a
transfer that moved nothing, a balance update that erased the APR, a retry that
debited twice because the server's day had rolled over and the user's had not.

The assertions are on the user-visible outcome — the balance, the stored row,
the response the model reads back — not on internal call shapes.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings

from apps.platform_logs.models import ToolContractEvent
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .models import FinanceAccount, FinanceSnapshot, FinanceTransaction
from .services import AmbiguousAccount, record_transaction, resolve_account


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class _RuntimeFinanceTestCase(TestCase):
    """Shared runtime-auth plumbing for the wave-1 endpoints."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Wave1 Finance", telegram_chat_id=900810)
        seed_internal_key(self.tenant)

    def _headers(self, tenant_id=None, key="test-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def _url(self, suffix):
        return f"/api/v1/finance/runtime/{self.tenant.id}{suffix}"

    def _post(self, suffix, payload):
        return self.client.post(
            self._url(suffix),
            data=payload,
            content_type="application/json",
            **self._headers(),
        )

    def _account(self, **kw):
        defaults = dict(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.CREDIT_CARD,
            nickname="Chase Card",
            current_balance=Decimal("1000.00"),
            is_active=True,
        )
        defaults.update(kw)
        return FinanceAccount.objects.create(**defaults)

    def _savings(self, **kw):
        savings = dict(
            account_type=FinanceAccount.AccountType.EMERGENCY_FUND,
            nickname="Emergency Fund",
            current_balance=Decimal("2000.00"),
        )
        savings.update(kw)
        return self._account(**savings)

    def _reasons(self):
        return set(ToolContractEvent.objects.filter(namespace="finance").values_list("reason_code", flat=True))


class AssetAccountVerbTests(_RuntimeFinanceTestCase):
    """Fix 1 — deposits/withdrawals exist; debt verbs stop eating asset money."""

    def test_deposit_into_emergency_fund_adds(self):
        """The headline bug: $500 into savings SUBTRACTED, clamped at 0."""
        account = self._savings(current_balance=Decimal("300.00"))

        response = self._post(
            "/transactions/", {"account_nickname": "Emergency Fund", "amount": 500, "transaction_type": "deposit"}
        )

        self.assertEqual(response.status_code, 201)
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("800.00"))
        self.assertEqual(response.json()["new_balance"], "800.00")
        self.assertIn("deposit_recorded", self._reasons())

    def test_payment_against_asset_account_is_refused(self):
        account = self._savings()

        response = self._post(
            "/transactions/", {"account_nickname": "Emergency Fund", "amount": 500, "transaction_type": "payment"}
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "asset_account_wrong_verb")
        self.assertIn("deposit", body["details"][0]["allowed"])
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("2000.00"))
        self.assertFalse(FinanceTransaction.objects.exists())
        self.assertIn("asset_payment_rejected", self._reasons())

    def test_refund_against_asset_account_is_refused(self):
        self._savings()
        response = self._post(
            "/transactions/", {"account_nickname": "Emergency Fund", "amount": 50, "transaction_type": "refund"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "asset_account_wrong_verb")

    def test_deposit_against_debt_account_is_refused(self):
        """Symmetric hole: 'deposit' on a credit card is ambiguous — pay it down
        or charge it up? — so it must not be guessed either."""
        account = self._account()

        response = self._post(
            "/transactions/", {"account_nickname": "Chase Card", "amount": 100, "transaction_type": "deposit"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "debt_account_wrong_verb")
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("1000.00"))

    def test_withdrawal_subtracts(self):
        account = self._savings()
        response = self._post(
            "/transactions/", {"account_nickname": "Emergency Fund", "amount": 250, "transaction_type": "withdrawal"}
        )
        self.assertEqual(response.status_code, 201)
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("1750.00"))

    def test_withdrawal_past_zero_is_refused_not_clamped(self):
        account = self._savings(current_balance=Decimal("100.00"))

        response = self._post(
            "/transactions/", {"account_nickname": "Emergency Fund", "amount": 500, "transaction_type": "withdrawal"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "withdrawal_exceeds_balance")
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("100.00"))
        self.assertFalse(FinanceTransaction.objects.exists())
        self.assertIn("withdrawal_rejected_overdraw", self._reasons())

    def test_transfer_is_refused_with_two_leg_guidance(self):
        """A transfer used to write a row, move nothing, and echo the untouched
        balance back as `new_balance` — a false success on both counts."""
        account = self._account()

        response = self._post(
            "/transactions/", {"account_nickname": "Chase Card", "amount": 300, "transaction_type": "transfer"}
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "transfer_unsupported")
        self.assertIn("withdrawal", body["message"])
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("1000.00"))
        self.assertFalse(FinanceTransaction.objects.exists())
        self.assertIn("transfer_unsupported", self._reasons())

    def test_unknown_transaction_type_is_refused_not_coerced(self):
        self._account()
        response = self._post(
            "/transactions/", {"account_nickname": "Chase Card", "amount": 10, "transaction_type": "magic"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unknown_transaction_type")
        self.assertFalse(FinanceTransaction.objects.exists())

    def test_transaction_type_case_is_normalized(self):
        """'Payment' is unambiguous — normalise it rather than refusing a
        client that only differs in case."""
        account = self._account()
        response = self._post(
            "/transactions/", {"account_nickname": "Chase Card", "amount": 100, "transaction_type": " Payment "}
        )
        self.assertEqual(response.status_code, 201)
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("900.00"))

    def test_payment_on_debt_still_reduces_balance(self):
        account = self._account()
        response = self._post("/transactions/", {"account_nickname": "Chase Card", "amount": 250})
        self.assertEqual(response.status_code, 201)
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("750.00"))


class AccountPartialUpdateTests(_RuntimeFinanceTestCase):
    """Fix 2 + 6 — an update writes only what it was given."""

    def _seed(self):
        return self._post(
            "/accounts/",
            {
                "nickname": "Chase Card",
                "account_type": "credit_card",
                "current_balance": 4200,
                "interest_rate": 22.9,
                "minimum_payment": 120,
                "credit_limit": 9000,
                "due_day": 15,
            },
        )

    def test_balance_only_update_preserves_every_other_field(self):
        """'My Chase card is now $3,800' used to blank the APR, the minimum
        payment, the credit limit and the due day in one call."""
        self.assertEqual(self._seed().status_code, 201)

        response = self._post("/accounts/", {"nickname": "Chase Card", "current_balance": 3800})

        self.assertEqual(response.status_code, 200)
        account = FinanceAccount.objects.get(nickname="Chase Card")
        self.assertEqual(account.current_balance, Decimal("3800.00"))
        self.assertEqual(account.interest_rate, Decimal("22.90"))
        self.assertEqual(account.minimum_payment, Decimal("120.00"))
        self.assertEqual(account.credit_limit, Decimal("9000.00"))
        self.assertEqual(account.due_day, 15)
        self.assertEqual(account.account_type, "credit_card")
        self.assertIn("partial_update_fields", self._reasons())

    def test_explicit_null_still_clears_a_field(self):
        self._seed()
        response = self._post("/accounts/", {"nickname": "Chase Card", "interest_rate": None})
        self.assertEqual(response.status_code, 200)
        account = FinanceAccount.objects.get(nickname="Chase Card")
        self.assertIsNone(account.interest_rate)
        self.assertEqual(account.current_balance, Decimal("4200.00"))

    def test_update_without_balance_is_allowed(self):
        self._seed()
        response = self._post("/accounts/", {"nickname": "Chase Card", "due_day": 3})
        self.assertEqual(response.status_code, 200)
        account = FinanceAccount.objects.get(nickname="Chase Card")
        self.assertEqual(account.due_day, 3)
        self.assertEqual(account.current_balance, Decimal("4200.00"))

    def test_create_requires_account_type(self):
        response = self._post("/accounts/", {"nickname": "Mystery", "current_balance": 100})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "account_type_required")
        self.assertFalse(FinanceAccount.objects.exists())
        self.assertIn("account_type_missing", self._reasons())

    def test_create_requires_balance(self):
        response = self._post("/accounts/", {"nickname": "Mystery", "account_type": "savings"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "current_balance_required")

    def test_explicit_null_balance_is_refused_not_written(self):
        """current_balance is NOT NULL — a null must be a 400, never a 500."""
        self._seed()
        response = self._post("/accounts/", {"nickname": "Chase Card", "current_balance": None})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "current_balance_required")
        self.assertEqual(FinanceAccount.objects.get().current_balance, Decimal("4200.00"))

    def test_explicit_null_account_type_leaves_the_type_alone(self):
        self._seed()
        response = self._post("/accounts/", {"nickname": "Chase Card", "account_type": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FinanceAccount.objects.get().account_type, "credit_card")

    def test_original_balance_is_set_on_create(self):
        self._seed()
        account = FinanceAccount.objects.get(nickname="Chase Card")
        self.assertEqual(account.original_balance, Decimal("4200.00"))


class DueDayBoundsTests(_RuntimeFinanceTestCase):
    """Fix 3 — the writer refuses a due_day that is not a day of the month."""

    def _create_with_due_day(self, due_day):
        return self._post(
            "/accounts/",
            {"nickname": "Loan", "account_type": "student_loan", "current_balance": 100, "due_day": due_day},
        )

    def test_zero_is_refused(self):
        response = self._create_with_due_day(0)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "due_day_out_of_range")
        self.assertFalse(FinanceAccount.objects.exists())
        self.assertIn("due_day_out_of_range", self._reasons())

    def test_thirty_two_is_refused(self):
        self.assertEqual(self._create_with_due_day(32).status_code, 400)

    def test_unparseable_is_refused_not_silently_nulled(self):
        response = self._create_with_due_day("whenever")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "due_day_invalid")

    def test_valid_day_is_stored(self):
        self.assertEqual(self._create_with_due_day(28).status_code, 201)
        self.assertEqual(FinanceAccount.objects.get().due_day, 28)


class InterestRateBoundsTests(_RuntimeFinanceTestCase):
    """Fix 4 — an APR is a percentage, storable, and never invented."""

    def _create_with_rate(self, rate):
        return self._post(
            "/accounts/",
            {"nickname": "Card", "account_type": "credit_card", "current_balance": 100, "interest_rate": rate},
        )

    def test_rate_above_one_hundred_is_a_400_not_a_500(self):
        """1000 parsed fine and then blew up inside the DecimalField."""
        response = self._create_with_rate(1000)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "apr_out_of_band")
        self.assertIn("apr_out_of_band", self._reasons())

    def test_negative_rate_is_refused(self):
        self.assertEqual(self._create_with_rate(-5).status_code, 400)

    def test_fractional_form_is_refused(self):
        """0.229 for "22.9%" would round to 0.23 and record a rate 100x too small."""
        response = self._create_with_rate(0.229)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "apr_precision")
        self.assertIn("22.9", response.json()["message"])

    def test_unparseable_rate_is_refused_not_nulled(self):
        response = self._create_with_rate("about twenty percent")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "apr_unparseable")
        self.assertIn("apr_unparseable", self._reasons())

    def test_percentage_form_is_accepted(self):
        self.assertEqual(self._create_with_rate(22.9).status_code, 201)
        self.assertEqual(FinanceAccount.objects.get().interest_rate, Decimal("22.90"))

    def test_zero_rate_is_accepted(self):
        self.assertEqual(self._create_with_rate(0).status_code, 201)

    def test_omitted_rate_stays_null(self):
        response = self._post("/accounts/", {"nickname": "Card", "account_type": "credit_card", "current_balance": 100})
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(FinanceAccount.objects.get().interest_rate)

    def test_payoff_flags_null_apr_treated_as_zero(self):
        self._account(interest_rate=None, minimum_payment=Decimal("50"))
        response = self._post("/payoff/calculate/", {"monthly_budget": 300, "strategy": "avalanche"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("null_apr_as_zero", self._reasons())


class AmbiguousNicknameTests(_RuntimeFinanceTestCase):
    """Fix 7 — a name that matches two accounts is a question, not a guess."""

    def test_two_matches_are_refused_with_candidates(self):
        self._account(nickname="Chase Freedom")
        self._account(nickname="Chase Sapphire")

        response = self._post("/transactions/", {"account_nickname": "Chase", "amount": 100})

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "ambiguous_account")
        self.assertEqual(
            sorted(body["details"][0]["candidates"]),
            ["Chase Freedom", "Chase Sapphire"],
        )
        self.assertFalse(FinanceTransaction.objects.exists())
        self.assertIn("ambiguous_account", self._reasons())

    def test_single_fuzzy_match_still_resolves(self):
        account = self._account(nickname="Chase Sapphire")
        response = self._post("/transactions/", {"account_nickname": "Chase", "amount": 100})
        self.assertEqual(response.status_code, 201)
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("900.00"))

    def test_exact_match_wins_over_a_second_partial_match(self):
        """'Chase' exactly names one account and is a substring of another; the
        exact tier resolves cleanly, so this is not ambiguous."""
        exact = self._account(nickname="Chase")
        self._account(nickname="Chase Sapphire")
        response = self._post("/transactions/", {"account_nickname": "Chase", "amount": 100})
        self.assertEqual(response.status_code, 201)
        exact.refresh_from_db()
        self.assertEqual(exact.current_balance, Decimal("900.00"))

    def test_balance_update_shares_the_ambiguity_guard(self):
        self._account(nickname="Chase Freedom")
        self._account(nickname="Chase Sapphire")
        response = self._post("/balance/", {"account_nickname": "Chase", "new_balance": 10})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "ambiguous_account")

    def test_archive_shares_the_ambiguity_guard(self):
        self._account(nickname="Chase Freedom")
        self._account(nickname="Chase Sapphire")
        response = self._post("/accounts/archive/", {"account_nickname": "Chase"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(FinanceAccount.objects.filter(is_active=True).count(), 2)

    def test_resolve_account_raises_ambiguous(self):
        self._account(nickname="Chase Freedom")
        self._account(nickname="Chase Sapphire")
        with self.assertRaises(AmbiguousAccount) as ctx:
            resolve_account(self.tenant, account_nickname="Chase")
        self.assertEqual(len(ctx.exception.candidates), 2)


class TenantLocalTransactionDateTests(_RuntimeFinanceTestCase):
    """Fix 5 — the dedup key holds across the tenant's morning.

    The dedup key includes the date. With a bare UTC ``date.today()`` a retry of
    the same payment sent minutes apart in Tokyo — 08:55 and 09:05 JST, either
    side of 00:00 UTC — produced two different keys, so the second call was
    accepted as a new payment and debited the account a second time.
    """

    def setUp(self):
        super().setUp()
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])

    def _pay_at(self, utc_moment):
        with patch("django.utils.timezone.now", return_value=utc_moment):
            return self._post("/transactions/", {"account_nickname": "Chase Card", "amount": 147})

    def test_retry_across_utc_midnight_dedups(self):
        account = self._account()
        before = datetime(2026, 8, 19, 23, 55, tzinfo=ZoneInfo("UTC"))  # 08:55 JST on the 20th
        after = datetime(2026, 8, 20, 0, 5, tzinfo=ZoneInfo("UTC"))  # 09:05 JST on the 20th

        first = self._pay_at(before)
        second = self._pay_at(after)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(FinanceTransaction.objects.count(), 1)
        self.assertEqual(FinanceTransaction.objects.get().date, date(2026, 8, 20))
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal("853.00"))

    def test_recorded_date_is_the_tenants_day_not_the_servers(self):
        self._account()
        self._pay_at(datetime(2026, 8, 19, 23, 55, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(FinanceTransaction.objects.get().date, date(2026, 8, 20))
        self.assertIn("tenant_date_applied", self._reasons())

    def test_explicit_date_still_wins(self):
        self._account()
        with patch("django.utils.timezone.now", return_value=datetime(2026, 8, 19, 23, 55, tzinfo=ZoneInfo("UTC"))):
            response = self._post(
                "/transactions/", {"account_nickname": "Chase Card", "amount": 147, "date": "2026-08-01"}
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(FinanceTransaction.objects.get().date, date(2026, 8, 1))

    def test_service_default_date_is_tenant_local(self):
        account = self._account()
        with patch("django.utils.timezone.now", return_value=datetime(2026, 8, 19, 23, 55, tzinfo=ZoneInfo("UTC"))):
            record_transaction(tenant=self.tenant, account=account, amount=Decimal("10"), writer="background")
        self.assertEqual(FinanceTransaction.objects.get().date, date(2026, 8, 20))


@override_settings(GRAVITY_ENABLED=True)
class SnapshotTenantMonthTests(TestCase):
    """Fix 5 — the monthly snapshot is filed under the tenant's month."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Wave1 Snap", telegram_chat_id=900811)
        Tenant.objects.filter(pk=self.tenant.pk).update(finance_enabled=True, status=Tenant.Status.ACTIVE)
        self.tenant.refresh_from_db()
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])
        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.CREDIT_CARD,
            nickname="Card",
            current_balance=Decimal("500.00"),
        )

    def test_snapshot_date_uses_tenant_local_month(self):
        from .snapshot import create_monthly_snapshots

        # 22:00 UTC on Aug 31 is already Sept 1 in Tokyo — the cron fires there.
        with patch("django.utils.timezone.now", return_value=datetime(2026, 8, 31, 22, 0, tzinfo=ZoneInfo("UTC"))):
            created = create_monthly_snapshots()

        self.assertEqual(created, 1)
        self.assertEqual(FinanceSnapshot.objects.get().date, date(2026, 9, 1))
