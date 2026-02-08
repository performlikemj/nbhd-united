"""Tests for tenants app."""
from django.test import TestCase

from .models import Tenant, User
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
