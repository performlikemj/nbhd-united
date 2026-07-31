"""Tests for the unmanaged-prefix Postgres CronJob repair command."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.cron.models import CronJob
from apps.tenants.models import Tenant, User


def _make_tenant(username: str) -> Tenant:
    user = User.objects.create_user(username=username, password="x")
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_id=f"oc-{username}",
        container_fqdn=f"oc-{username}.internal",
        postgres_cron_canonical=True,
    )


class RepairFuelCronRowsCommandTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant("repair-fuel")

    def _row(
        self,
        name: str,
        *,
        tenant: Tenant | None = None,
        enabled: bool = True,
        managed: bool = True,
    ) -> CronJob:
        return CronJob.objects.create(
            tenant=tenant or self.tenant,
            name=name,
            enabled=enabled,
            managed=managed,
            data={},
        )

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_default_dry_run_reports_matches_without_writing(self, mock_gateway):
        fuel = self._row("_fuel:welcome")
        sync = self._row("_sync:phase-2")
        before = {row.id: (row.enabled, row.managed, row.updated_at) for row in (fuel, sync)}
        stdout = StringIO()

        call_command(
            "repair_fuel_cron_rows",
            "--tenant",
            str(self.tenant.id),
            stdout=stdout,
        )

        for row in (fuel, sync):
            row.refresh_from_db()
            self.assertEqual(
                (row.enabled, row.managed, row.updated_at),
                before[row.id],
            )
        output = stdout.getvalue()
        self.assertIn("name | id | status | created | action", output)
        self.assertIn("_fuel:welcome", output)
        self.assertIn("_sync:phase-2", output)
        self.assertIn("would set enabled=false, managed=false", output)
        self.assertIn(
            f"repair_fuel_cron_rows: tenant={self.tenant.id} matched=2 retired=0 already_retired=0",
            output,
        )
        mock_gateway.assert_not_called()

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_confirm_retires_matching_rows_and_is_idempotent(self, mock_gateway):
        fuel = self._row("_fuel:welcome")
        sync = self._row("_sync:phase-2")
        already_retired = self._row(
            "_fuel:old-program",
            enabled=False,
            managed=False,
        )
        first_stdout = StringIO()

        call_command(
            "repair_fuel_cron_rows",
            "--tenant",
            str(self.tenant.id),
            "--confirm",
            stdout=first_stdout,
        )

        for row in (fuel, sync, already_retired):
            row.refresh_from_db()
            self.assertFalse(row.enabled)
            self.assertFalse(row.managed)
        self.assertIn(
            f"repair_fuel_cron_rows: tenant={self.tenant.id} matched=3 retired=2 already_retired=1",
            first_stdout.getvalue(),
        )
        timestamps = {row.id: row.updated_at for row in (fuel, sync, already_retired)}

        second_stdout = StringIO()
        call_command(
            "repair_fuel_cron_rows",
            "--tenant",
            str(self.tenant.id),
            "--confirm",
            stdout=second_stdout,
        )

        for row in (fuel, sync, already_retired):
            row.refresh_from_db()
            self.assertEqual(row.updated_at, timestamps[row.id])
        self.assertIn(
            f"repair_fuel_cron_rows: tenant={self.tenant.id} matched=3 retired=0 already_retired=3",
            second_stdout.getvalue(),
        )
        mock_gateway.assert_not_called()

    def test_confirm_leaves_normal_names_and_other_tenants_untouched(self):
        normal = self._row("Morning Briefing")
        near_match = self._row("_fuels:welcome")
        other_tenant = _make_tenant("repair-fuel-other")
        other_tenant_fuel = self._row(
            "_fuel:welcome",
            tenant=other_tenant,
        )

        call_command(
            "repair_fuel_cron_rows",
            "--tenant",
            str(self.tenant.id),
            "--confirm",
            stdout=StringIO(),
        )

        for row in (normal, near_match, other_tenant_fuel):
            row.refresh_from_db()
            self.assertTrue(row.enabled)
            self.assertTrue(row.managed)

    def test_refuses_to_run_without_tenant(self):
        with self.assertRaises(CommandError):
            call_command("repair_fuel_cron_rows", stdout=StringIO())
