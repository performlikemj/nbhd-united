import hashlib
import hmac

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User
from apps.tenants.pat_models import PersonalAccessToken, generate_pat
from apps.yardtalk.models import License, LicenseActivation

VALID_KEY = "YT-ABCD-1234-WXYZ"


@override_settings(YARDTALK_LICENSE_RECEIPT_SECRET="test-receipt-secret")
class LicenseValidateTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.license = License.objects.create(
            key=VALID_KEY,
            purchaser_email="buyer@example.com",
            stripe_session_id="cs_validate",
        )

    def validate(self, license_key=VALID_KEY, device_id="device-1", **extra):
        payload = {
            "license_key": license_key,
            "device_id": device_id,
            **extra,
        }
        return self.client.post(
            "/api/v1/yardtalk/licenses/validate/",
            payload,
            format="json",
            REMOTE_ADDR="203.0.113.10",
        )

    def test_new_devices_consume_three_seats(self):
        self.assertEqual(self.validate(device_id="device-1").json()["seats_remaining"], 2)
        self.assertEqual(self.validate(device_id="device-2").json()["seats_remaining"], 1)
        self.assertEqual(self.validate(device_id="device-3").json()["seats_remaining"], 0)
        self.assertEqual(self.license.activations.count(), 3)

    def test_repeat_device_is_idempotent_with_same_receipt(self):
        first = self.validate(device_id="device-repeat")
        second = self.validate(device_id="device-repeat")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["receipt"], second.json()["receipt"])
        self.assertEqual(first.json()["seats_remaining"], 2)
        self.assertEqual(second.json()["seats_remaining"], 2)
        self.assertEqual(self.license.activations.count(), 1)

        expected = hmac.new(
            b"test-receipt-secret",
            f"{VALID_KEY}:device-repeat".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(first.json()["receipt"], expected)

    def test_fourth_device_hits_seat_limit_but_existing_device_stays_valid(self):
        for device_id in ("device-1", "device-2", "device-3"):
            self.validate(device_id=device_id)

        fourth = self.validate(device_id="device-4")
        existing = self.validate(device_id="device-1")

        self.assertEqual(fourth.status_code, 200)
        self.assertEqual(fourth.json(), {"valid": False, "reason": "seat_limit"})
        self.assertEqual(existing.status_code, 200)
        self.assertTrue(existing.json()["valid"])
        self.assertEqual(existing.json()["seats_remaining"], 0)
        self.assertEqual(self.license.activations.count(), 3)

    def test_unknown_key(self):
        response = self.validate(license_key="YT-0000-0000-0000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"valid": False, "reason": "unknown_key"})

    def test_revoked_license(self):
        self.license.status = License.Status.REVOKED
        self.license.save(update_fields=["status"])
        response = self.validate()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"valid": False, "reason": "revoked"})
        self.assertFalse(LicenseActivation.objects.exists())

    def test_malformed_payloads_return_400(self):
        malformed_payloads = [
            {},
            {"license_key": VALID_KEY},
            {"device_id": "device-1"},
            {"license_key": "yt-abcd-1234-wxyz", "device_id": "device-1"},
            {"license_key": "YTABCD1234WXYZ", "device_id": "device-1"},
            {"license_key": "YT-ABCD-1234-WXY!", "device_id": "device-1"},
            {"license_key": VALID_KEY, "device_id": ""},
            {"license_key": VALID_KEY, "device_id": "x" * 65},
        ]
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/v1/yardtalk/licenses/validate/",
                    payload,
                    format="json",
                    REMOTE_ADDR="203.0.113.11",
                )
                self.assertEqual(response.status_code, 400)

        invalid_json = self.client.post(
            "/api/v1/yardtalk/licenses/validate/",
            data=b"{",
            content_type="application/json",
            REMOTE_ADDR="203.0.113.11",
        )
        self.assertEqual(invalid_json.status_code, 400)

    def test_validate_endpoint_throttles_per_ip(self):
        for _ in range(30):
            response = self.validate(device_id="same-device")
            self.assertEqual(response.status_code, 200)

        response = self.validate(device_id="same-device")
        self.assertEqual(response.status_code, 429)


class EntitlementTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def create_user(self, suffix: str, **tenant_fields):
        user = User.objects.create_user(
            username=f"yardtalk-{suffix}",
            email=f"{suffix}@example.com",
            password="test-password-123",
        )
        Tenant.objects.create(user=user, **tenant_fields)
        return user

    def create_pat(self, user, scopes):
        raw_token, prefix, token_hash = generate_pat()
        PersonalAccessToken.objects.create(
            user=user,
            name="YardTalk",
            token_prefix=prefix,
            token_hash=token_hash,
            scopes=scopes,
        )
        return raw_token

    def entitlement(self, raw_token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
        return self.client.get("/api/v1/yardtalk/entitlement/")

    def test_active_subscription_is_entitled(self):
        user = self.create_user(
            "active",
            status=Tenant.Status.ACTIVE,
            stripe_subscription_id="sub_active",
            is_trial=False,
        )
        response = self.entitlement(self.create_pat(user, ["yardtalk:read"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "entitled": True,
                "source": "subscription",
                "recheck_after_days": 7,
            },
        )

    def test_no_subscription_is_not_entitled(self):
        user = self.create_user(
            "none",
            status=Tenant.Status.ACTIVE,
            stripe_subscription_id="",
            is_trial=False,
        )
        response = self.entitlement(self.create_pat(user, ["yardtalk:read"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"entitled": False, "source": "none"})

    def test_trial_or_inactive_subscription_is_not_entitled(self):
        trial_user = self.create_user(
            "trial",
            status=Tenant.Status.ACTIVE,
            stripe_subscription_id="sub_trial",
            is_trial=True,
        )
        self.assertFalse(self.entitlement(self.create_pat(trial_user, ["yardtalk:read"])).json()["entitled"])

        inactive_user = self.create_user(
            "inactive",
            status=Tenant.Status.SUSPENDED,
            stripe_subscription_id="sub_inactive",
            is_trial=False,
        )
        self.assertFalse(self.entitlement(self.create_pat(inactive_user, ["yardtalk:read"])).json()["entitled"])

    def test_invalid_pat_returns_401(self):
        response = self.entitlement("pat_invalid-token")
        self.assertEqual(response.status_code, 401)

    def test_missing_scope_returns_403(self):
        user = self.create_user(
            "scope",
            status=Tenant.Status.ACTIVE,
            stripe_subscription_id="sub_scope",
        )
        response = self.entitlement(self.create_pat(user, ["sessions:read"]))
        self.assertEqual(response.status_code, 403)

    def test_revoked_pat_returns_401(self):
        user = self.create_user("revoked", status=Tenant.Status.ACTIVE)
        raw_token = self.create_pat(user, ["yardtalk:read"])
        PersonalAccessToken.objects.filter(user=user).update(revoked_at=timezone.now())
        response = self.entitlement(raw_token)
        self.assertEqual(response.status_code, 401)
