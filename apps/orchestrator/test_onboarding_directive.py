"""Delivery tests for the tenant-conditional profile onboarding directive."""

from django.test import TestCase

from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant

_ONBOARDING_DIRECTIVE = (
    "If USER.md lacks timezone or home city, ask once and never infer; save via nbhd_update_profile."
)


class OnboardingDirectiveTests(TestCase):
    def _tenant(self, *, timezone: str, city: str, chat_id: int):
        tenant = create_tenant(display_name="Onboarding Directive", telegram_chat_id=chat_id)
        tenant.user.timezone = timezone
        tenant.user.location_city = city
        tenant.user.save(update_fields=["timezone", "location_city"])
        return tenant

    def test_renders_when_timezone_or_home_city_is_omitted_from_user_md(self):
        profiles = (
            ("UTC", "Tokyo", 920201),
            ("Asia/Tokyo", "", 920202),
        )
        for timezone, city, chat_id in profiles:
            tenant = self._tenant(timezone=timezone, city=city, chat_id=chat_id)
            with self.subTest(timezone=timezone, city=city):
                rendered = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
                self.assertIn(_ONBOARDING_DIRECTIVE, rendered)

    def test_absent_when_timezone_and_home_city_are_both_present(self):
        tenant = self._tenant(timezone="Asia/Tokyo", city="Tokyo", chat_id=920203)
        rendered = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
        self.assertNotIn(_ONBOARDING_DIRECTIVE, rendered)

    def test_absent_from_the_base_template_without_a_tenant(self):
        rendered = render_workspace_files("neighbor")["NBHD_AGENTS_MD"]
        self.assertNotIn(_ONBOARDING_DIRECTIVE, rendered)
