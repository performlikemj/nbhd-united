"""Tests for the one-time grandfathered welcome-stamp cleanup."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


@override_settings(GRAVITY_ENABLED=True)
class StampGrandfatheredWelcomesCommandTest(TestCase):
    def _tenant(
        self,
        *,
        suffix: int,
        status: str = Tenant.Status.ACTIVE,
        fuel: bool = False,
        finance: bool = False,
        core: bool = False,
        marks: dict | None = None,
    ) -> Tenant:
        tenant = create_tenant(
            display_name=f"Grandfather {suffix}",
            telegram_chat_id=909000 + suffix,
        )
        tenant.status = status
        tenant.fuel_enabled = fuel
        tenant.finance_enabled = finance
        tenant.core_enabled = core
        tenant.welcomes_sent = marks or {}
        tenant.save(
            update_fields=[
                "status",
                "fuel_enabled",
                "finance_enabled",
                "core_enabled",
                "welcomes_sent",
            ]
        )
        return tenant

    def test_stamps_only_missing_enabled_keys_and_leaves_inactive_tenants_untouched(self):
        existing_fuel_stamp = "2026-05-08T01:02:03+00:00"
        active = self._tenant(
            suffix=1,
            fuel=True,
            finance=True,
            core=True,
            marks={"fuel": existing_fuel_stamp},
        )
        inactive = self._tenant(
            suffix=2,
            status=Tenant.Status.SUSPENDED,
            fuel=True,
            finance=True,
            core=True,
        )
        disabled = self._tenant(suffix=3)

        output = StringIO()
        with patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_gateway:
            call_command("stamp_grandfathered_welcomes", stdout=output)

        active.refresh_from_db()
        inactive.refresh_from_db()
        disabled.refresh_from_db()
        self.assertEqual(active.welcomes_sent["fuel"], existing_fuel_stamp)
        self.assertIn("finance", active.welcomes_sent)
        self.assertIn("core", active.welcomes_sent)
        self.assertEqual(inactive.welcomes_sent, {})
        self.assertEqual(disabled.welcomes_sent, {})
        self.assertIn("Stamped 2 welcome key(s) across 1 of 2 active tenant(s)", output.getvalue())
        mock_gateway.assert_not_called()

    def test_dry_run_reports_counts_without_mutating(self):
        tenant = self._tenant(suffix=4, fuel=True, finance=True, core=True)

        output = StringIO()
        call_command("stamp_grandfathered_welcomes", "--dry-run", stdout=output)

        tenant.refresh_from_db()
        self.assertEqual(tenant.welcomes_sent, {})
        self.assertIn("Would stamp 3 welcome key(s) across 1 of 1 active tenant(s)", output.getvalue())
        self.assertIn("fuel=1, finance=1, core=1", output.getvalue())

    def test_second_run_is_idempotent(self):
        tenant = self._tenant(suffix=5, fuel=True)

        call_command("stamp_grandfathered_welcomes", stdout=StringIO())
        first_stamp = Tenant.objects.get(pk=tenant.pk).welcomes_sent["fuel"]
        output = StringIO()
        call_command("stamp_grandfathered_welcomes", stdout=output)

        tenant.refresh_from_db()
        self.assertEqual(tenant.welcomes_sent["fuel"], first_stamp)
        self.assertIn("Stamped 0 welcome key(s) across 0 of 1 active tenant(s)", output.getvalue())
