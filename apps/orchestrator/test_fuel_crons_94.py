"""Fuel prep crons on OpenClaw 9.4: signed file, not the gated gateway path."""

from unittest.mock import patch

from django.test import TestCase

from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator.fuel_cron import regenerate_fuel_crons
from apps.orchestrator.test_openclaw_round_ten import live_copy, system_row
from apps.orchestrator.test_tenant_openclaw_migration import tenant_fixture


def fuel_row(name="_fuel:Old Plan"):
    return {
        "name": name,
        "enabled": True,
        "payload": {"kind": "agentTurn", "message": "Synthetic prep"},
        "delivery": {"mode": "none"},
        "schedule": {"tz": "Asia/Tokyo", "expr": "0 6 * * *", "kind": "cron"},
        "sessionTarget": "isolated",
    }


class FuelReconcileOn94Tests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(951101)

    def test_9_4_tenant_rewrites_signed_file_and_never_calls_gateway(self):
        self.tenant.openclaw_version = "2026.9.4"
        self.tenant.save(update_fields=["openclaw_version"])
        with (
            patch("apps.cron.share_cron_sync.write_tenant_crons_file", return_value=1) as write,
            patch("apps.cron.gateway_client.invoke_gateway_tool") as gateway,
        ):
            regenerate_fuel_crons(self.tenant)
        write.assert_called_once_with(self.tenant)
        gateway.assert_not_called()

    def test_5_28_tenant_still_uses_gateway(self):
        with (
            patch("apps.cron.share_cron_sync.write_tenant_crons_file") as write,
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}) as gateway,
        ):
            regenerate_fuel_crons(self.tenant)
        write.assert_not_called()
        self.assertTrue(gateway.called)


class FuelMirrorMigrationTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(951102)
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        with suppress_cronjob_reconcile():
            CronJob.objects.create(tenant=self.tenant, name="Morning Briefing", data=system_row(), managed=True)
            CronJob.objects.create(tenant=self.tenant, name="_fuel:Old Plan", data=fuel_row(), managed=True)

    def test_stale_fuel_mirror_rows_do_not_block(self):
        m.preservation_precheck(self.tenant, [live_copy()])

    def test_live_fuel_copy_is_not_imported_proof(self):
        live_fuel = {**live_copy("_fuel:Live Only"), "delivery": {"mode": "announce", "to": "private"}}
        m.preservation_precheck(self.tenant, [live_copy(), live_fuel])
