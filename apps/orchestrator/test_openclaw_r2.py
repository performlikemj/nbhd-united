"""Round two regressions. All Azure, gateway and share operations are offline."""

import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.utils import timezone

from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import hibernation as h
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator.cron_declarations import supported_declaration
from apps.orchestrator.test_tenant_openclaw_migration import job, record, tenant_fixture
from apps.tenants.admin import TenantAdmin
from apps.tenants.models import Tenant


class RoundTwoTests(TestCase):
    def setUp(self):
        # Some broader-suite environment probes replace AZURE_MOCK. Pin our
        # offline transport mode rather than relying on process startup state.
        env = patch.dict(os.environ, {"AZURE_MOCK": "true"})
        env.start()
        self.addCleanup(env.stop)
        self.tenant = tenant_fixture(949402)

    def test_f1_legacy_suspend_error_keeps_main_call_sequence(self):
        # origin/main including #1649: capture -> suspend (error tolerated) ->
        # deactivate -> DB mark -> schedule. Never abort-resume on 5.28.
        calls = []
        with (
            patch.object(h, "_capture_tenant_cron_schedules", side_effect=lambda t: calls.append("capture") or []),
            patch(
                "apps.cron.suspension.suspend_tenant_crons",
                side_effect=lambda t: calls.append("suspend") or {"errors": 1},
            ),
            patch("apps.cron.suspension.resume_tenant_crons") as resume,
            patch(
                "apps.orchestrator.azure_client.hibernate_container_app",
                side_effect=lambda t: calls.append("deactivate"),
            ),
            patch.object(h, "_schedule_next_cron_wake", side_effect=lambda *a: calls.append("schedule")),
        ):
            self.assertTrue(h.hibernate_idle_tenant(self.tenant))
        self.assertEqual(calls, ["capture", "suspend", "deactivate", "schedule"])
        resume.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.hibernated_at)

    def test_legacy_call_trace_identical_to_pinned_origin_main(self):
        baseline = json.loads(Path(__file__).with_name("fixtures").joinpath("legacy_hibernation_main.json").read_text())
        for fault in (None, "suspend", "deactivate"):
            traces = []
            for reference in (True, False):
                events = Mock()
                tenant = SimpleNamespace(
                    id="legacy",
                    pk="legacy",
                    container_id="oc-test",
                    container_fqdn="test.invalid",
                    openclaw_version="2026.5.28",
                    openclaw_migration={"status": "FAILED"},
                )
                with (
                    patch.object(h, "Tenant") as model,
                    patch.object(h, "logger") as logger,
                    patch.object(h, "_capture_tenant_cron_schedules", return_value=[job()]) as capture,
                    patch.object(h, "_schedule_next_cron_wake") as wake,
                    patch(
                        "apps.cron.suspension.suspend_tenant_crons",
                        side_effect=RuntimeError("failure") if fault == "suspend" else None,
                        return_value={"disabled": 1},
                    ) as suspend,
                    patch("apps.cron.suspension.resume_tenant_crons") as resume,
                    patch(
                        "apps.orchestrator.azure_client.hibernate_container_app",
                        side_effect=RuntimeError("failure") if fault == "deactivate" else None,
                    ) as deactivate,
                    patch.object(h.timezone, "now", return_value="fixed-time"),
                ):
                    for name, mock in (
                        ("db", model),
                        ("log", logger),
                        ("capture", capture),
                        ("schedule", wake),
                        ("suspend", suspend),
                        ("resume", resume),
                        ("deactivate", deactivate),
                    ):
                        events.attach_mock(mock, name)
                    if reference:
                        namespace = dict(h.__dict__)
                        exec(baseline["functions"]["hibernate_idle_tenant"], namespace)
                        result = namespace["hibernate_idle_tenant"](tenant)
                    else:
                        result = h.hibernate_idle_tenant(tenant)
                    traces.append((result, events.mock_calls))
            with self.subTest(fault=fault):
                self.assertEqual(traces[0], traces[1])

    def test_f4_hourly_runtime_anchor_is_supported(self):
        declaration = job(schedule={"kind": "every", "everyMs": 3600000, "anchorMs": 1700000000123})
        self.assertTrue(supported_declaration(declaration))
        declaration["payload"]["command"] = "forbidden"
        self.assertFalse(supported_declaration(declaration))

    def test_f5_imminent_capture_is_nonmutating(self):
        before = Tenant.objects.filter(pk=self.tenant.pk).values().get()
        imminent = job(schedule={"kind": "at", "at": (timezone.now() + timedelta(minutes=5)).isoformat()})
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [imminent]}),
            self.assertRaisesRegex(m.MigrationError, "cron_imminent"),
        ):
            m.capture(self.tenant, record())
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())
        self.assertEqual(Tenant.objects.filter(pk=self.tenant.pk).values().get(), before)

    def test_f5_retry_preserves_dashboard_edit_and_deletion(self):
        rec = record()
        source = job(schedule={"kind": "every", "everyMs": 86400000})
        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [source]}):
            m.capture(self.tenant, rec)
            row = CronJob.objects.get(tenant=self.tenant, name=source["name"])
            with suppress_cronjob_reconcile():
                row.enabled = False
                row.data["payload"]["message"] = "newer dashboard edit"
                row.save()
            # A disabled canonical row stays in Postgres and no longer blocks
            # (round ten); the retry must still not overwrite the edit.
            m.capture(self.tenant, rec)
            row.refresh_from_db()
            self.assertFalse(row.enabled)
            self.assertEqual(row.data["payload"]["message"], "newer dashboard edit")
            with suppress_cronjob_reconcile():
                row.delete()
            m.capture(self.tenant, rec)
            self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name=source["name"]).exists())

    def test_f5_rolled_back_import_does_not_keep_ownership_versions(self):
        rec = record()
        original_save = m._save

        def save(tenant, value):
            if "imported_versions" in value:
                raise RuntimeError("checkpoint database error")
            original_save(tenant, value)

        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [job()]}),
            patch.object(m, "_save", side_effect=save),
            self.assertRaisesRegex(RuntimeError, "checkpoint database error"),
        ):
            m.capture(self.tenant, rec)
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())
        self.assertNotIn("imported_versions", rec)

    def test_f6_fresh_recapture_refuses_unprojectable_source_cancellation(self):
        rec = record()
        source = job(schedule={"kind": "at", "at": (timezone.now() + timedelta(hours=3)).isoformat()})
        with patch("apps.cron.gateway_client.invoke_gateway_tool", side_effect=[{"jobs": [source]}, {"jobs": []}]):
            m.capture(self.tenant, rec)
            before = CronJob.objects.get(tenant=self.tenant).data
            with self.assertRaisesRegex(m.MigrationError, "source_cancellation_not_projected"):
                m.capture(self.tenant, rec)
        self.assertEqual(CronJob.objects.get(tenant=self.tenant).data, before)

    def test_f7_admin_excludes_private_payloads(self):
        model_admin = TenantAdmin(Tenant, AdminSite())
        request = Mock()
        fields = model_admin.get_form(request, self.tenant).base_fields
        self.assertNotIn("openclaw_migration", fields)
