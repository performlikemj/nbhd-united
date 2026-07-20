"""YardTalk licensing tests — webhook mint/email, license validate + device
seats, and subscription entitlement.

Mirrors ``test_webhooks``/``test_credits`` for webhook signature + dispatch
mocking. Throttles are cache-backed, so every test class clears the cache in
setUp. Literal paths, no reverse().
"""

from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.signing import loads
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import YardTalkLicense
from apps.billing.yardtalk_licensing import LICENSE_RECEIPT_SALT, is_yardtalk_entitled
from apps.tenants.models import Tenant, User
from apps.tenants.pat_models import PersonalAccessToken, generate_pat

_KEY_RE = r"^YT-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$"


@override_settings(DJSTRIPE_WEBHOOK_SECRET="whsec_test")
class YardTalkWebhookMintTest(TestCase):
    def setUp(self):
        cache.clear()

    def _event(
        self,
        *,
        session_id="cs_yt_1",
        kind="yardtalk_license",
        email="buyer@example.com",
        customer_email=None,
        pi="pi_yt_1",
        event_type="checkout.session.completed",
        mode="payment",
    ):
        obj = {"id": session_id, "mode": mode, "payment_intent": pi}
        obj["metadata"] = {"kind": kind} if kind else {}
        if email is not None:
            obj["customer_details"] = {"email": email}
        if customer_email is not None:
            obj["customer_email"] = customer_email
        return {"id": f"evt_{session_id}", "type": event_type, "data": {"object": obj}}

    def _fire(self, event):
        with patch("apps.billing.views.stripe.Webhook.construct_event", return_value=event):
            return self.client.post(
                "/api/v1/billing/webhook/",
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="sig",
            )

    def test_mint_and_email(self):
        resp = self._fire(self._event())
        self.assertEqual(resp.status_code, 200)
        lic = YardTalkLicense.objects.get(stripe_session_id="cs_yt_1")
        self.assertRegex(lic.key, _KEY_RE)
        # Alphabet excludes I/L/O/U.
        for ch in lic.key.replace("-", "")[2:]:
            self.assertNotIn(ch, "ILOU")
        self.assertEqual(lic.stripe_payment_intent_id, "pi_yt_1")
        self.assertIsNotNone(lic.key_email_sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(lic.key, mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["buyer@example.com"])

    def test_replayed_event_no_double_mint_no_reemail(self):
        self._fire(self._event())
        self._fire(self._event())  # same session id
        self.assertEqual(YardTalkLicense.objects.filter(stripe_session_id="cs_yt_1").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_async_payment_succeeded_also_mints(self):
        resp = self._fire(self._event(session_id="cs_yt_async", event_type="checkout.session.async_payment_succeeded"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(YardTalkLicense.objects.filter(stripe_session_id="cs_yt_async").exists())

    def test_customer_email_fallback(self):
        # No customer_details.email — fall back to top-level customer_email.
        resp = self._fire(self._event(session_id="cs_yt_fb", email=None, customer_email="fallback@example.com"))
        self.assertEqual(resp.status_code, 200)
        lic = YardTalkLicense.objects.get(stripe_session_id="cs_yt_fb")
        self.assertEqual(lic.email, "fallback@example.com")

    def test_payment_mode_without_kind_no_license(self):
        resp = self._fire(self._event(session_id="cs_yt_nokind", kind=None))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(YardTalkLicense.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_missing_email_no_mint_no_crash(self):
        resp = self._fire(self._event(session_id="cs_yt_noemail", email=None, customer_email=None))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(YardTalkLicense.objects.exists())
        self.assertEqual(len(mail.outbox), 0)


class YardTalkValidateTest(TestCase):
    VALID_KEY = "YT-ABCD-2345-EFGH"

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.lic = YardTalkLicense.objects.create(key=self.VALID_KEY, email="v@example.com", stripe_session_id="cs_v1")

    def _post(self, key, device="dev-1"):
        return self.client.post(
            "/api/v1/yardtalk/licenses/validate/",
            {"license_key": key, "device_id": device},
            format="json",
        )

    def test_unknown_key(self):
        resp = self._post("YT-0000-0000-0000")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"valid": False, "reason": "unknown_key"})

    def test_revoked_key(self):
        YardTalkLicense.objects.filter(id=self.lic.id).update(revoked_at=timezone.now())
        resp = self._post(self.VALID_KEY)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"valid": False, "reason": "revoked"})

    def test_happy_path(self):
        resp = self._post(self.VALID_KEY, device="dev-1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["valid"])
        self.assertEqual(data["seats_remaining"], 2)
        payload = loads(data["receipt"], salt=LICENSE_RECEIPT_SALT)
        self.assertEqual(payload["lic"], str(self.lic.id))
        self.assertEqual(payload["dev"], "dev-1")
        self.assertIn("iat", payload)

    def test_seat_flow(self):
        r1 = self._post(self.VALID_KEY, device="d1")
        self.assertEqual(r1.json()["seats_remaining"], 2)
        r2 = self._post(self.VALID_KEY, device="d2")
        self.assertEqual(r2.json()["seats_remaining"], 1)
        r3 = self._post(self.VALID_KEY, device="d3")
        self.assertEqual(r3.json()["seats_remaining"], 0)
        r4 = self._post(self.VALID_KEY, device="d4")
        self.assertEqual(r4.json(), {"valid": False, "reason": "seat_limit"})
        # Re-validating an already-registered device consumes no seat.
        r1b = self._post(self.VALID_KEY, device="d1")
        self.assertTrue(r1b.json()["valid"])
        self.assertEqual(r1b.json()["seats_remaining"], 0)

    def test_normalization_accepts_messy_input(self):
        # lowercase, hyphenless, whitespace-padded all resolve to the same key.
        self.assertTrue(self._post("  yt-abcd-2345-efgh  ", device="dn1").json()["valid"])
        self.assertTrue(self._post("ytABCD2345EFGH", device="dn1").json()["valid"])
        self.assertTrue(self._post("YT ABCD 2345 EFGH", device="dn1").json()["valid"])

    def test_missing_license_key_400(self):
        resp = self.client.post("/api/v1/yardtalk/licenses/validate/", {"device_id": "d"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_missing_device_id_400(self):
        resp = self.client.post("/api/v1/yardtalk/licenses/validate/", {"license_key": self.VALID_KEY}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_device_id_too_long_400(self):
        resp = self._post(self.VALID_KEY, device="x" * 65)
        self.assertEqual(resp.status_code, 400)

    def test_per_key_throttle_429_on_sixth(self):
        for _ in range(5):
            resp = self._post(self.VALID_KEY, device="d")
            self.assertEqual(resp.status_code, 200)
        resp6 = self._post(self.VALID_KEY, device="d")
        self.assertEqual(resp6.status_code, 429)


class YardTalkEntitlementPredicateTest(TestCase):
    """Direct predicate matrix (endpoint-independent)."""

    def _tenant(self, slug, **kw):
        user = User.objects.create_user(username=slug, email=f"{slug}@t.test", password="x" * 32)
        return Tenant.objects.create(user=user, **kw)

    def test_active_sub_not_trial_entitled(self):
        t = self._tenant("e1", status=Tenant.Status.ACTIVE, stripe_subscription_id="sub_1", is_trial=False)
        self.assertTrue(is_yardtalk_entitled(t))

    def test_trial_not_entitled(self):
        t = self._tenant("e2", status=Tenant.Status.ACTIVE, stripe_subscription_id="sub_2", is_trial=True)
        self.assertFalse(is_yardtalk_entitled(t))

    def test_suspended_not_entitled(self):
        t = self._tenant("e3", status=Tenant.Status.SUSPENDED, stripe_subscription_id="sub_3", is_trial=False)
        self.assertFalse(is_yardtalk_entitled(t))

    def test_budget_exempt_active_no_sub_entitled(self):
        t = self._tenant("e4", status=Tenant.Status.ACTIVE, is_budget_exempt=True)
        self.assertTrue(is_yardtalk_entitled(t))

    def test_none_not_entitled(self):
        self.assertFalse(is_yardtalk_entitled(None))


class YardTalkEntitlementEndpointTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _tenant(self, slug, **kw):
        user = User.objects.create_user(username=slug, email=f"{slug}@t.test", password="x" * 32)
        tenant = Tenant.objects.create(user=user, **kw)
        return tenant, user

    def _pat(self, user, scopes=("yardtalk:read",)):
        raw, prefix, token_hash = generate_pat()
        PersonalAccessToken.objects.create(
            user=user, name="yt", token_prefix=prefix, token_hash=token_hash, scopes=list(scopes)
        )
        return raw

    def _get(self, raw):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return self.client.get("/api/v1/yardtalk/entitlement/")

    def test_active_sub_returns_entitled_subscription(self):
        tenant, user = self._tenant("ep1", status=Tenant.Status.ACTIVE, stripe_subscription_id="sub_1", is_trial=False)
        resp = self._get(self._pat(user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"entitled": True, "source": "subscription", "recheck_after_days": 14})

    def test_trial_returns_not_entitled(self):
        tenant, user = self._tenant("ep2", status=Tenant.Status.ACTIVE, stripe_subscription_id="sub_2", is_trial=True)
        resp = self._get(self._pat(user))
        self.assertEqual(resp.json(), {"entitled": False, "source": "none", "recheck_after_days": 14})

    def test_suspended_returns_not_entitled(self):
        tenant, user = self._tenant(
            "ep3", status=Tenant.Status.SUSPENDED, stripe_subscription_id="sub_3", is_trial=False
        )
        self.assertFalse(self._get(self._pat(user)).json()["entitled"])

    def test_budget_exempt_returns_entitled(self):
        tenant, user = self._tenant("ep4", status=Tenant.Status.ACTIVE, is_budget_exempt=True)
        self.assertTrue(self._get(self._pat(user)).json()["entitled"])

    def test_user_without_tenant_returns_not_entitled(self):
        user = User.objects.create_user(username="ep5", email="ep5@t.test", password="x" * 32)
        resp = self._get(self._pat(user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"entitled": False, "source": "none", "recheck_after_days": 14})

    # ── auth matrix ────────────────────────────────────────────────────────
    def test_pat_missing_scope_403(self):
        tenant, user = self._tenant("ap1", status=Tenant.Status.ACTIVE, stripe_subscription_id="sub_x")
        resp = self._get(self._pat(user, scopes=("sessions:read",)))
        self.assertEqual(resp.status_code, 403)

    def test_pat_with_scope_200(self):
        tenant, user = self._tenant("ap2", status=Tenant.Status.ACTIVE, stripe_subscription_id="sub_y")
        resp = self._get(self._pat(user))
        self.assertEqual(resp.status_code, 200)

    def test_no_auth_401(self):
        resp = APIClient().get("/api/v1/yardtalk/entitlement/")
        self.assertEqual(resp.status_code, 401)

    def test_jwt_console_user_bypasses_scope_200(self):
        tenant, user = self._tenant("ap4", status=Tenant.Status.ACTIVE, stripe_subscription_id="sub_z")
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        resp = self.client.get("/api/v1/yardtalk/entitlement/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["entitled"])


class YardTalkPATMintScopeTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_mint_accepts_yardtalk_read_scope(self):
        user = User.objects.create_user(username="mint1", email="mint1@t.test", password="x" * 32)
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        resp = self.client.post(
            "/api/v1/auth/tokens/create/",
            {"name": "YardTalk", "scopes": ["yardtalk:read"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["scopes"], ["yardtalk:read"])
