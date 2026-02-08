"""Tests for router app."""
from django.test import TestCase

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from .services import clear_cache, extract_chat_id, resolve_container


class ExtractChatIdTest(TestCase):
    def test_from_message(self):
        update = {"message": {"chat": {"id": 123}}}
        self.assertEqual(extract_chat_id(update), 123)

    def test_from_callback_query(self):
        update = {"callback_query": {"message": {"chat": {"id": 456}}}}
        self.assertEqual(extract_chat_id(update), 456)

    def test_empty_update(self):
        self.assertIsNone(extract_chat_id({}))


class ResolveContainerTest(TestCase):
    def setUp(self):
        clear_cache()
        self.tenant = create_tenant(display_name="Router Test", telegram_chat_id=777888999)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-test.internal.azurecontainerapps.io"
        self.tenant.save()

    def tearDown(self):
        clear_cache()

    def test_resolves_active_tenant(self):
        fqdn = resolve_container(777888999)
        self.assertEqual(fqdn, "oc-test.internal.azurecontainerapps.io")

    def test_returns_none_for_unknown(self):
        self.assertIsNone(resolve_container(000000000))

    def test_caches_result(self):
        resolve_container(777888999)
        # Second call should use cache (no DB hit)
        fqdn = resolve_container(777888999)
        self.assertEqual(fqdn, "oc-test.internal.azurecontainerapps.io")
