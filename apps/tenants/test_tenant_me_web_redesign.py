"""web_redesign in the current-tenant payload (WEB_REDESIGN_TENANT_IDS gate)."""

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.chat_gates import web_redesign_enabled
from apps.tenants.services import create_tenant


class TenantMeWebRedesignTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Web Redesign Test", telegram_chat_id=555444222)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _flag(self):
        cache.clear()  # /tenants/me/ is tenant-cached for 60 s
        resp = self.client.get("/api/v1/tenants/me/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("web_redesign", resp.data)
        return resp.data["web_redesign"]

    @override_settings(WEB_REDESIGN_TENANT_IDS="")
    def test_empty_means_nobody(self):
        self.assertIs(self._flag(), False)

    def test_listed_tenant_only(self):
        other = "00000000-0000-0000-0000-000000000001"
        with override_settings(WEB_REDESIGN_TENANT_IDS=f"{other}, {self.tenant.id}"):
            self.assertIs(self._flag(), True)
        with override_settings(WEB_REDESIGN_TENANT_IDS=other):
            self.assertIs(self._flag(), False)

    def test_exact_star_is_everyone_but_contained_star_is_ignored(self):
        with override_settings(WEB_REDESIGN_TENANT_IDS=" * "):
            self.assertIs(self._flag(), True)
        with override_settings(WEB_REDESIGN_TENANT_IDS="*x,bogus"):
            self.assertIs(self._flag(), False)

    def test_helper_fails_closed_for_missing_tenant(self):
        with override_settings(WEB_REDESIGN_TENANT_IDS="*"):
            self.assertIs(web_redesign_enabled(None), False)
