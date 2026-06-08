"""Prepaid-credit tests — covers the money-safety must-fixes from the design critique.

Idempotency, server-derived amounts, race-safe non-negative debit, the credit-aware
gate (without defeating the 402 breaker), monthly-reset preservation, reconcile
double-debit, refund clawback, unpaid/unknown/no-tenant webhook handling, and the
subscription-routing regression.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient

from apps.billing import credits
from apps.billing.constants import ANTHROPIC_SONNET_MODEL, CREDIT_PACKS, MINIMAX_MODEL
from apps.billing.models import CreditLedger
from apps.billing.services import check_budget, record_usage
from apps.tenants.models import Tenant, User
from apps.tenants.services import reset_monthly_counters

_STRIPE = dict(STRIPE_LIVE_MODE=False, STRIPE_TEST_SECRET_KEY="sk_test_x", FRONTEND_URL="https://app.test")


def _tenant(slug, *, credit="0", estimated="0", budget="0", exempt=False, customer="", pi=""):
    user = User.objects.create_user(username=slug, password="x" * 32, email=f"{slug}@t.test")
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        purchased_credit=Decimal(credit),
        estimated_cost_this_month=Decimal(estimated),
        monthly_cost_budget=Decimal(budget),
        is_budget_exempt=exempt,
        stripe_customer_id=customer,
    )


def _bal(tenant):
    tenant.refresh_from_db(fields=["purchased_credit"])
    return tenant.purchased_credit


class PackInvariantTest(TestCase):
    def test_price_never_below_credit(self):
        self.assertTrue(CREDIT_PACKS)
        for pid, p in CREDIT_PACKS.items():
            self.assertGreaterEqual(
                p["price_cents"],
                int(p["credit_dollars"] * 100),
                f"{pid}: price {p['price_cents']} < credit {p['credit_dollars']}*100 (would lose money)",
            )


class GrantTest(TestCase):
    def test_grant_increments_and_ledgers(self):
        t = _tenant("g1")
        applied = credits.grant_credit(
            tenant=t, credit_dollars=Decimal("5"), stripe_event_id="evt_1", pack_id="credit_5"
        )
        self.assertTrue(applied)
        self.assertEqual(_bal(t), Decimal("5.0000"))  # Decimal round-trip
        self.assertEqual(CreditLedger.objects.filter(tenant=t, kind="grant").count(), 1)

    def test_grant_idempotent_on_duplicate_event(self):
        t = _tenant("g2")
        self.assertTrue(credits.grant_credit(tenant=t, credit_dollars=Decimal("5"), stripe_event_id="evt_dup"))
        self.assertFalse(credits.grant_credit(tenant=t, credit_dollars=Decimal("5"), stripe_event_id="evt_dup"))
        self.assertEqual(_bal(t), Decimal("5.0000"))
        self.assertEqual(CreditLedger.objects.filter(tenant=t, kind="grant").count(), 1)

    def test_grant_without_event_id_refused(self):
        t = _tenant("g3")
        self.assertFalse(credits.grant_credit(tenant=t, credit_dollars=Decimal("5"), stripe_event_id=""))
        self.assertEqual(_bal(t), Decimal("0.0000"))


class DebitTest(TestCase):
    def test_overage_only_debit(self):
        # cap 5, prior 4.50, a $1 turn → only $0.50 (the part above cap) drawn.
        t = _tenant("d1", credit="10", estimated="5.50", budget="5")
        drawn = credits.debit_overage_credit(t.id, Decimal("4.50"), Decimal("5.50"), Decimal("5"))
        self.assertEqual(drawn, Decimal("0.5000"))
        self.assertEqual(_bal(t), Decimal("9.5000"))

    def test_below_cap_no_debit(self):
        t = _tenant("d2", credit="10", budget="5")
        self.assertEqual(credits.debit_overage_credit(t.id, Decimal("0"), Decimal("3"), Decimal("5")), Decimal("0"))
        self.assertEqual(_bal(t), Decimal("10.0000"))

    def test_never_goes_negative(self):
        # draw bigger than balance → draws only the balance, clamps at 0.
        t = _tenant("d3", credit="0.30", estimated="5.80", budget="5")
        drawn = credits.debit_overage_credit(t.id, Decimal("5.00"), Decimal("5.80"), Decimal("5"))
        self.assertEqual(drawn, Decimal("0.3000"))
        self.assertEqual(_bal(t), Decimal("0.0000"))

    def test_reconcile_successive_passes_no_double_debit(self):
        t = _tenant("d4", credit="10", budget="5")
        self.assertEqual(
            credits.debit_overage_credit(t.id, Decimal("4"), Decimal("6"), Decimal("5")), Decimal("1.0000")
        )
        # next reconcile pass: provider_truth unchanged → before==after → draws 0
        self.assertEqual(credits.debit_overage_credit(t.id, Decimal("6"), Decimal("6"), Decimal("5")), Decimal("0"))
        self.assertEqual(_bal(t), Decimal("9.0000"))


class RecordUsageIntegrationTest(TestCase):
    def test_overage_drawn_from_credit(self):
        # budget 0.01 so any real cost is overage; MiniMax $0.28/1M input.
        t = _tenant("r1", credit="10", budget="0.01")
        record_usage(t, "message", input_tokens=1_000_000, output_tokens=0, model_used=MINIMAX_MODEL)
        self.assertEqual(_bal(t), Decimal("9.7300"))  # 10 - (0.28 - 0.01)

    def test_byo_call_never_debits(self):
        t = _tenant("r2", credit="10", budget="0.01")
        record_usage(t, "message", input_tokens=1_000_000, output_tokens=0, model_used=ANTHROPIC_SONNET_MODEL)
        self.assertEqual(_bal(t), Decimal("10.0000"))

    def test_system_call_never_debits(self):
        t = _tenant("r3", credit="10", budget="0.01")
        record_usage(t, "message", input_tokens=1_000_000, output_tokens=0, model_used=MINIMAX_MODEL, is_system=True)
        self.assertEqual(_bal(t), Decimal("10.0000"))

    def test_exempt_tenant_never_debits(self):
        t = _tenant("r4", credit="10", budget="0.01", exempt=True)
        record_usage(t, "message", input_tokens=1_000_000, output_tokens=0, model_used=MINIMAX_MODEL)
        self.assertEqual(_bal(t), Decimal("10.0000"))


class GateTest(TestCase):
    def test_over_included_with_credit_allowed(self):
        t = _tenant("gate1", credit="5", estimated="6", budget="5")
        self.assertTrue(t.is_over_budget)  # pure: included-cap only
        self.assertEqual(check_budget(t), "")  # credit extends → allowed

    def test_over_included_no_credit_blocked(self):
        t = _tenant("gate2", credit="0", estimated="6", budget="5")
        self.assertEqual(check_budget(t), "personal")

    def test_within_included_allowed(self):
        t = _tenant("gate3", credit="0", estimated="3", budget="5")
        self.assertEqual(check_budget(t), "")

    def test_is_over_budget_stays_pure(self):
        # The 402 breaker / threshold emails depend on is_over_budget meaning
        # "over the included cap" regardless of credit.
        t = _tenant("gate4", credit="100", estimated="6", budget="5")
        self.assertTrue(t.is_over_budget)
        self.assertTrue(t.has_spendable_budget)


class MonthlyResetTest(TestCase):
    def test_reset_preserves_purchased_credit(self):
        t = _tenant("m1", credit="10", estimated="5", budget="5")
        # reset_monthly_counters only resets tenants who sent messages.
        Tenant.objects.filter(id=t.id).update(messages_this_month=3)
        reset_monthly_counters()
        t.refresh_from_db()
        self.assertEqual(t.estimated_cost_this_month, Decimal("0.0000"))  # included reset
        self.assertEqual(t.purchased_credit, Decimal("10.0000"))  # purchased preserved


class RefundTest(TestCase):
    def test_refund_claws_back_and_clamps(self):
        t = _tenant("rf1", credit="0")
        credits.grant_credit(
            tenant=t, credit_dollars=Decimal("5"), stripe_event_id="evt_g", stripe_payment_intent_id="pi_1"
        )
        self.assertEqual(_bal(t), Decimal("5.0000"))
        # full refund
        credits.handle_credit_refund("evt_r", {"payment_intent": "pi_1", "amount": 600, "amount_refunded": 600})
        self.assertEqual(_bal(t), Decimal("0.0000"))

    def test_refund_idempotent(self):
        t = _tenant("rf2", credit="0")
        credits.grant_credit(
            tenant=t, credit_dollars=Decimal("5"), stripe_event_id="evt_g2", stripe_payment_intent_id="pi_2"
        )
        credits.handle_credit_refund("evt_r2", {"payment_intent": "pi_2", "amount": 600, "amount_refunded": 600})
        credits.handle_credit_refund("evt_r2", {"payment_intent": "pi_2", "amount": 600, "amount_refunded": 600})
        self.assertEqual(_bal(t), Decimal("0.0000"))
        self.assertEqual(CreditLedger.objects.filter(tenant=t, kind="reversal").count(), 1)

    def test_refund_clamps_when_already_spent(self):
        t = _tenant("rf3", credit="0")
        credits.grant_credit(
            tenant=t, credit_dollars=Decimal("5"), stripe_event_id="evt_g3", stripe_payment_intent_id="pi_3"
        )
        # spend it all: prior already at cap, so the whole $5 is overage.
        credits.debit_overage_credit(t.id, Decimal("5"), Decimal("10"), Decimal("5"))
        self.assertEqual(_bal(t), Decimal("0.0000"))
        credits.handle_credit_refund("evt_r3", {"payment_intent": "pi_3", "amount": 600, "amount_refunded": 600})
        self.assertEqual(_bal(t), Decimal("0.0000"))  # clamped, never negative


class WebhookHandlerTest(TestCase):
    def _session(self, t, *, pack="credit_5", paid=True, tampered=None):
        meta = {"kind": "credit_topup", "pack_id": pack, "tenant_id": str(t.id)}
        if tampered is not None:
            meta["credit_dollars"] = tampered
        return {
            "id": "cs_1",
            "mode": "payment",
            "payment_status": "paid" if paid else "unpaid",
            "payment_intent": "pi_x",
            "amount_total": 600,
            "customer": "cus_x",
            "metadata": meta,
        }

    def test_grant_uses_server_pack_not_tampered_metadata(self):
        t = _tenant("w1")
        credits.handle_credit_topup_completed("evt_w1", self._session(t, pack="credit_5", tampered="9999"))
        self.assertEqual(_bal(t), Decimal("5.0000"))  # pack value, NOT 9999

    def test_unpaid_session_no_grant(self):
        t = _tenant("w2")
        credits.handle_credit_topup_completed("evt_w2", self._session(t, paid=False))
        self.assertEqual(_bal(t), Decimal("0.0000"))

    def test_unknown_pack_no_grant(self):
        t = _tenant("w3")
        credits.handle_credit_topup_completed("evt_w3", self._session(t, pack="nope"))
        self.assertEqual(_bal(t), Decimal("0.0000"))

    def test_no_tenant_no_exception(self):
        s = {
            "id": "cs_2",
            "mode": "payment",
            "payment_status": "paid",
            "metadata": {
                "kind": "credit_topup",
                "pack_id": "credit_5",
                "tenant_id": "00000000-0000-0000-0000-000000000000",
            },
        }
        credits.handle_credit_topup_completed("evt_w4", s)  # must not raise

    def test_persists_customer_id(self):
        t = _tenant("w5")
        credits.handle_credit_topup_completed("evt_w5", self._session(t))
        t.refresh_from_db()
        self.assertEqual(t.stripe_customer_id, "cus_x")


class WebhookDispatchTest(TestCase):
    """must-fix #2: a credit top-up must never reach the subscription handler."""

    def _fire(self, event):
        factory = RequestFactory()
        req = factory.post("/api/v1/billing/webhook/", data=b"{}", content_type="application/json")
        from apps.billing import views

        with (
            patch.object(views.stripe.Webhook, "construct_event", return_value=event),
            patch("apps.billing.views.handle_credit_topup_completed") as credit_h,
            patch("apps.billing.views.handle_checkout_completed") as sub_h,
        ):
            resp = views.stripe_webhook(req)
        return resp, credit_h, sub_h

    def test_credit_topup_routes_to_credit_handler(self):
        event = {
            "id": "evt_d1",
            "type": "checkout.session.completed",
            "data": {"object": {"mode": "payment", "payment_status": "paid", "metadata": {"kind": "credit_topup"}}},
        }
        resp, credit_h, sub_h = self._fire(event)
        self.assertEqual(resp.status_code, 200)
        credit_h.assert_called_once()
        sub_h.assert_not_called()

    def test_subscription_still_routes_to_subscription_handler(self):
        event = {
            "id": "evt_d2",
            "type": "checkout.session.completed",
            "data": {"object": {"mode": "subscription", "metadata": {"tier": "starter"}}},
        }
        resp, credit_h, sub_h = self._fire(event)
        self.assertEqual(resp.status_code, 200)
        sub_h.assert_called_once()
        credit_h.assert_not_called()


@override_settings(**_STRIPE)
class CheckoutEndpointTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_unknown_pack_400(self):
        t = _tenant("c1")
        self.client.force_authenticate(user=t.user)
        resp = self.client.post("/api/v1/billing/credits/checkout/", {"pack_id": "nope"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_rejected(self):
        resp = APIClient().post("/api/v1/billing/credits/checkout/", {"pack_id": "credit_5"}, format="json")
        self.assertIn(resp.status_code, (401, 403))

    @patch("apps.billing.views.stripe.checkout.Session.create")
    def test_uses_server_price_ignores_client_amount(self, mock_create):
        mock_create.return_value = type("S", (), {"url": "https://stripe.test/x"})()
        t = _tenant("c2")
        self.client.force_authenticate(user=t.user)
        resp = self.client.post(
            "/api/v1/billing/credits/checkout/",
            {"pack_id": "credit_5", "amount": 999999, "credit_dollars": "9999"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["mode"], "payment")
        # Server pack price, NOT the client's injected amount.
        self.assertEqual(kwargs["line_items"][0]["price_data"]["unit_amount"], CREDIT_PACKS["credit_5"]["price_cents"])
        self.assertEqual(kwargs["metadata"]["kind"], "credit_topup")
        self.assertEqual(kwargs["metadata"]["tenant_id"], str(t.id))


class BalanceEndpointTest(TestCase):
    def test_returns_balance_and_packs(self):
        t = _tenant("b1", credit="7")
        client = APIClient()
        client.force_authenticate(user=t.user)
        resp = client.get("/api/v1/billing/credits/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["purchased_credit"], "7.0000")
        self.assertTrue(resp.data["packs"])
