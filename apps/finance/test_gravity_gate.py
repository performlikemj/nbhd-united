"""Tests for the GRAVITY_ENABLED platform kill switch (privacy pause).

GRAVITY_ENABLED (settings) is a fail-safe-off product gate: while it is False
(the production default), Gravity is paused platform-wide regardless of any
tenant's stored ``finance_enabled`` flag. dev + test settings set it True so the
rest of the finance suite exercises the feature; these tests pin it explicitly
via ``override_settings`` to cover both states.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.orchestrator.config_generator import build_cron_seed_jobs
from apps.tenants.serializers import TenantSerializer
from apps.tenants.services import create_tenant


class FinanceActivePropertyTests(TestCase):
    """``Tenant.finance_active`` = stored flag AND the platform gate."""

    def setUp(self):
        self.tenant = create_tenant(display_name="GravityGate", telegram_chat_id=900777)
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])

    @override_settings(GRAVITY_ENABLED=False)
    def test_pause_overrides_enabled_flag(self):
        # The whole point of the kill switch: enabled flag set, but paused wins.
        self.assertTrue(self.tenant.finance_enabled)
        self.assertFalse(self.tenant.finance_active)

    @override_settings(GRAVITY_ENABLED=True)
    def test_active_when_platform_and_tenant_on(self):
        self.assertTrue(self.tenant.finance_active)

    @override_settings(GRAVITY_ENABLED=True)
    def test_inactive_when_tenant_off_even_if_platform_on(self):
        self.tenant.finance_enabled = False
        self.tenant.save(update_fields=["finance_enabled"])
        self.assertFalse(self.tenant.finance_active)


@override_settings(GRAVITY_ENABLED=False)
class GravityPausedGatingTests(TestCase):
    """With the platform paused, no finance surface reaches the assistant."""

    def setUp(self):
        self.tenant = create_tenant(display_name="GravityPaused", telegram_chat_id=900778)
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])

    def test_weekly_checkin_cron_absent(self):
        names = {j["name"] for j in build_cron_seed_jobs(self.tenant)}
        self.assertNotIn("Gravity Weekly Check-in", names)

    def test_finance_and_insights_plugins_absent_from_config(self):
        from apps.orchestrator.config_generator import config_to_json, generate_openclaw_config

        config_json = config_to_json(generate_openclaw_config(self.tenant))
        self.assertNotIn("nbhd-finance-tools", config_json)
        self.assertNotIn("nbhd-insights-tools", config_json)

    def test_finance_envelope_section_disabled(self):
        # The "## Gravity — finance state" USER.md section is gated at the
        # registry level on finance_active, so it never renders while paused.
        import apps.finance.envelope  # noqa: F401  (force section registration)
        from apps.orchestrator.envelope_registry import all_sections

        finance_section = next(s for s in all_sections() if s.key == "finance")
        self.assertFalse(finance_section.enabled(self.tenant))

    def test_serializer_reports_unavailable(self):
        self.assertFalse(TenantSerializer(self.tenant).data["gravity_available"])

    def test_weekly_reflection_analysis_skipped(self):
        # The one server-side job that sends finance data to an LLM for
        # analysis must short-circuit on the finance_active gate (synthesis.py)
        # BEFORE reaching the model call — even with finance data present.
        from apps.finance.models import FinanceAccount
        from apps.insights.synthesis import generate_weekly_reflection

        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.CREDIT_CARD,
            nickname="Paused CC",
            current_balance=Decimal("1234.56"),
        )
        result = generate_weekly_reflection(self.tenant)
        self.assertEqual(result.skipped, "finance_disabled")


@override_settings(GRAVITY_ENABLED=True)
class GravityActiveGatingTests(TestCase):
    """With the platform on, an enabled tenant gets the full finance surface."""

    def setUp(self):
        self.tenant = create_tenant(display_name="GravityActive", telegram_chat_id=900779)
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])

    def test_weekly_checkin_cron_present(self):
        names = {j["name"] for j in build_cron_seed_jobs(self.tenant)}
        self.assertIn("Gravity Weekly Check-in", names)

    def test_finance_plugin_present_in_config(self):
        from apps.orchestrator.config_generator import config_to_json, generate_openclaw_config

        config_json = config_to_json(generate_openclaw_config(self.tenant))
        self.assertIn("nbhd-finance-tools", config_json)

    def test_finance_envelope_section_enabled(self):
        import apps.finance.envelope  # noqa: F401
        from apps.orchestrator.envelope_registry import all_sections

        finance_section = next(s for s in all_sections() if s.key == "finance")
        self.assertTrue(finance_section.enabled(self.tenant))

    def test_serializer_reports_available(self):
        self.assertTrue(TenantSerializer(self.tenant).data["gravity_available"])


class FinanceSettingsEnableGuardTests(TestCase):
    """The enable endpoint refuses while paused; disabling is always allowed."""

    def setUp(self):
        self.tenant = create_tenant(display_name="GravityEnableGuard", telegram_chat_id=900780)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    @override_settings(GRAVITY_ENABLED=False)
    def test_enable_refused_when_paused(self):
        resp = self.client.patch("/api/v1/finance/settings/", {"finance_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"], "gravity_paused")
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.finance_enabled)

    @override_settings(GRAVITY_ENABLED=False)
    def test_disable_allowed_when_paused(self):
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])
        with patch("apps.cron.publish.publish_task"):
            resp = self.client.patch("/api/v1/finance/settings/", {"finance_enabled": False}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.finance_enabled)

    @override_settings(GRAVITY_ENABLED=True)
    def test_enable_allowed_when_platform_on(self):
        with patch("apps.cron.publish.publish_task"):
            resp = self.client.patch("/api/v1/finance/settings/", {"finance_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.finance_enabled)
