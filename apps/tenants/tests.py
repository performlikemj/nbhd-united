"""Tests for tenants app."""
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Tenant, User
from .serializers import TenantSerializer
from .services import create_tenant


class TenantModelTest(TestCase):
    def test_create_tenant(self):
        tenant = create_tenant(
            display_name="Test User",
            telegram_chat_id=123456789,
        )
        self.assertEqual(tenant.status, Tenant.Status.PENDING)
        self.assertEqual(tenant.user.telegram_chat_id, 123456789)
        self.assertEqual(tenant.user.display_name, "Test User")
        self.assertTrue(tenant.key_vault_prefix.startswith("tenants-"))

    def test_tenant_is_active(self):
        tenant = create_tenant(display_name="Test", telegram_chat_id=111)
        self.assertFalse(tenant.is_active)
        tenant.status = Tenant.Status.ACTIVE
        tenant.save()
        self.assertTrue(tenant.is_active)

    def test_tenant_budget(self):
        tenant = create_tenant(display_name="Test", telegram_chat_id=222)
        self.assertFalse(tenant.is_over_budget)
        tenant.tokens_this_month = tenant.monthly_token_budget
        tenant.save()
        self.assertTrue(tenant.is_over_budget)

    def test_unique_chat_id(self):
        create_tenant(display_name="User1", telegram_chat_id=333)
        with self.assertRaises(Exception):
            create_tenant(display_name="User2", telegram_chat_id=333)


class AuthLoginTest(TestCase):
    def setUp(self):
        self.email = "login@example.com"
        self.password = "testpass123"
        User.objects.create_user(
            username=self.email, email=self.email, password=self.password,
        )

    def test_login_with_email_returns_tokens(self):
        response = self.client.post(
            "/api/v1/auth/login/",
            {"email": self.email, "password": self.password},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)

    def test_login_with_wrong_password_returns_401(self):
        response = self.client.post(
            "/api/v1/auth/login/",
            {"email": self.email, "password": "wrongpass"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


class AuthLogoutTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="logout@example.com",
            email="logout@example.com",
            password="testpass123",
        )
        refresh = RefreshToken.for_user(self.user)
        self.refresh = str(refresh)
        self.access = str(refresh.access_token)
        self.auth_header = f"Bearer {self.access}"

    def test_logout_blacklists_refresh_token(self):
        response = self.client.post(
            "/api/v1/auth/logout/",
            {"refresh": self.refresh},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )
        self.assertEqual(response.status_code, 204)

        refresh_response = self.client.post(
            "/api/v1/auth/refresh/",
            {"refresh": self.refresh},
            content_type="application/json",
        )
        self.assertEqual(refresh_response.status_code, 401)

    def test_logout_requires_refresh_token(self):
        response = self.client.post(
            "/api/v1/auth/logout/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )
        self.assertEqual(response.status_code, 400)

    def test_logout_rejects_invalid_refresh_token(self):
        response = self.client.post(
            "/api/v1/auth/logout/",
            {"refresh": "not-a-token"},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )
        self.assertEqual(response.status_code, 400)


class TenantSerializerTest(TestCase):
    def test_active_with_subscription_returns_true(self):
        tenant = create_tenant(display_name="Sub", telegram_chat_id=500)
        tenant.stripe_subscription_id = "sub_x"
        tenant.status = Tenant.Status.ACTIVE
        tenant.save()
        data = TenantSerializer(tenant).data
        self.assertTrue(data["has_active_subscription"])

    def test_deleted_with_subscription_returns_false(self):
        tenant = create_tenant(display_name="Del", telegram_chat_id=501)
        tenant.stripe_subscription_id = "sub_x"
        tenant.status = Tenant.Status.DELETED
        tenant.save()
        data = TenantSerializer(tenant).data
        self.assertFalse(data["has_active_subscription"])

    def test_active_without_subscription_returns_false(self):
        tenant = create_tenant(display_name="NoSub", telegram_chat_id=502)
        tenant.stripe_subscription_id = ""
        tenant.status = Tenant.Status.ACTIVE
        tenant.save()
        data = TenantSerializer(tenant).data
        self.assertFalse(data["has_active_subscription"])
