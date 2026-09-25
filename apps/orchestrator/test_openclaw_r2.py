"""Round two regressions. All Azure, gateway and share operations are offline."""

import json
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.utils import timezone

from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import hibernation as h
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator import runtime_operator as op
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
                    cron_suspend_state={"active": True},
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

    def test_f5_explicit_pause_only_restores_previously_enabled_ids(self):
        rec = record()
        rec["cron_export"] = [job("on"), job("off", enabled=False)]
        observed = [dict(j, enabled=False) for j in rec["cron_export"]]
        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": observed}) as rpc:
            m.pause_source_crons(self.tenant, rec)
            m.resume_source_crons(self.tenant, rec)
        mutations = [c.args[2] for c in rpc.call_args_list if c.args[1] == "cron.update"]
        self.assertEqual(
            mutations, [{"jobId": "on-id", "patch": {"enabled": False}}, {"jobId": "on-id", "patch": {"enabled": True}}]
        )

    def test_f5_frequent_pause_allowed_but_imminent_one_shot_still_defers(self):
        m.assert_cutover_safe([job(schedule={"kind": "every", "everyMs": 900000})], pause_recurring=True)
        with self.assertRaisesRegex(m.MigrationError, "cron_imminent"):
            m.assert_cutover_safe(
                [job(schedule={"kind": "at", "at": (timezone.now() + timedelta(minutes=5)).isoformat()})],
                pause_recurring=True,
            )

    def test_f2_deactivate_timeout_has_durable_intent_and_wake(self):
        self.tenant.openclaw_version = "2026.9.4"
        self.tenant.save(update_fields=["openclaw_version"])

        def deactivate(*args):
            fresh = Tenant.objects.get(pk=self.tenant.pk)
            self.assertIsNotNone(fresh.hibernated_at)
            self.assertEqual(fresh.cron_suspend_state["deactivation"], "pending")
            raise TimeoutError()

        with (
            patch.object(h, "_capture_tenant_cron_schedules", return_value=[]),
            patch("apps.cron.suspension.suspend_tenant_crons", return_value={"errors": 0}),
            patch("apps.cron.suspension.resume_tenant_crons") as resume,
            patch("apps.orchestrator.azure_client.hibernate_container_app", side_effect=deactivate),
            patch("apps.orchestrator.azure_client.container_app_has_active_revision", return_value=False),
            patch("apps.cron.publish.publish_task") as publish,
            patch.object(h, "_schedule_next_cron_wake") as wake,
        ):
            self.assertTrue(h.hibernate_idle_tenant(self.tenant))
        resume.assert_not_called()
        wake.assert_called()
        publish.assert_called()
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.hibernated_at)

    def test_f4_hourly_runtime_anchor_is_supported(self):
        declaration = job(schedule={"kind": "every", "everyMs": 3600000, "anchorMs": 1700000000123})
        self.assertTrue(supported_declaration(declaration))
        declaration["payload"]["command"] = "forbidden"
        self.assertFalse(supported_declaration(declaration))

    def test_file_cron_unknown_state_has_payload_free_warning(self):
        self.tenant.openclaw_version = "2026.9.4"
        with (
            patch.object(op, "list_crons", side_effect=RuntimeError("private sentinel")),
            self.assertLogs("apps.orchestrator.hibernation", level="WARNING") as logs,
        ):
            self.assertEqual(h._cron_active_or_imminent(self.tenant), "cron_state_unknown")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("reason=cron_state_unknown", logs.output[0])
        self.assertNotIn("private sentinel", logs.output[0])
        self.assertNotIn("Traceback", logs.output[0])

    def test_f5_imminent_capture_is_nonmutating(self):
        before = Tenant.objects.filter(pk=self.tenant.pk).values().get()
        imminent = job(schedule={"kind": "every", "everyMs": 900000})
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [imminent]}),
            self.assertRaisesRegex(m.MigrationError, "cron_imminent"),
        ):
            m.capture(self.tenant, record())
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())
        self.assertEqual(Tenant.objects.filter(pk=self.tenant.pk).values().get(), before)

    def test_f2_post_azure_db_error_retains_marker_and_recovery(self):
        self.tenant.openclaw_version = "2026.9.4"
        self.tenant.save(update_fields=["openclaw_version"])
        original = h._update_tenant_after_azure

        def update(pk, **kwargs):
            if kwargs.get("cron_suspend_state", {}).get("deactivation") == "complete":
                raise RuntimeError("db failure after Azure")
            return original(pk, **kwargs)

        with (
            patch.object(h, "_capture_tenant_cron_schedules", return_value=[]),
            patch("apps.cron.suspension.suspend_tenant_crons", return_value={"errors": 0}),
            patch("apps.orchestrator.azure_client.hibernate_container_app"),
            patch("apps.orchestrator.azure_client.container_app_has_active_revision", return_value=False),
            patch("apps.cron.publish.publish_task") as publish,
            patch.object(h, "_schedule_next_cron_wake") as wake,
            patch.object(h, "_update_tenant_after_azure", side_effect=update),
        ):
            self.assertTrue(h.hibernate_idle_tenant(self.tenant))
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.hibernated_at)
        self.assertEqual(self.tenant.cron_suspend_state["deactivation"], "pending")
        self.assertEqual(wake.call_count, 2)
        publish.assert_called_once()

    def test_f2_completed_hibernation_recovery_callback_is_noop(self):
        self.tenant.openclaw_version = "2026.9.4"
        self.tenant.hibernated_at = timezone.now()
        self.tenant.cron_suspend_state = {"deactivation": "complete", "active": True}
        self.tenant.save()
        with patch.object(h, "wake_hibernated_tenant") as wake:
            self.assertEqual(
                h.wake_for_cron_task(str(self.tenant.pk), recovery_only=True)["status"], "recovery_not_needed"
            )
        wake.assert_not_called()

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

    def test_f6_fresh_recapture_audits_cancelled_one_shot(self):
        rec = record()
        source = job(schedule={"kind": "at", "at": (timezone.now() + timedelta(hours=3)).isoformat()})
        with patch("apps.cron.gateway_client.invoke_gateway_tool", side_effect=[{"jobs": [source]}, {"jobs": []}]):
            m.capture(self.tenant, rec)
            m.capture(self.tenant, rec)
        with patch.object(op, "list_crons", return_value=[]):
            m.one_shot_dispositions(self.tenant, rec)
        self.assertEqual(rec["one_shot_dispositions"][0]["disposition"], "cancelled_at_source")
        self.assertTrue(rec["source_resolutions"][source["id"]]["observed_at"])

    def test_f7_admin_excludes_private_payloads(self):
        model_admin = TenantAdmin(Tenant, AdminSite())
        request = Mock()
        fields = model_admin.get_form(request, self.tenant).base_fields
        self.assertNotIn("openclaw_migration", fields)
        self.assertNotIn("cron_suspend_state", fields)

    def test_f7_share_cleanup_does_not_require_replica(self):
        with (
            patch.object(op, "run_node", side_effect=op.OperatorError("offline")) as console,
            patch("apps.orchestrator.azure_client.delete_workspace_file") as delete,
            self.assertRaises(op.OperatorError),
        ):
            op.capture_cron_declarations(self.tenant)
        self.assertEqual(console.call_count, 1)
        delete.assert_called_once()

    @skipUnless(shutil.which("node"), "Node required for real adapter execution")
    def test_f3_recreated_equivalent_is_not_duplicated(self):
        desired = job()
        current = dict(desired, id="recreated-by-agent")
        with tempfile.TemporaryDirectory() as directory:

            def write(tid, name, *, data, **kw):
                Path(directory, name).write_bytes(data)

            def execute(tenant, body, **kw):
                body = body.replace(
                    "/opt/nbhd/nbhd-cron-sync.mjs", Path("runtime/openclaw/nbhd-cron-sync.mjs").resolve().as_uri()
                )
                script = (
                    "const fs=require('node:fs'),crypto=require('node:crypto');let rows="
                    + json.dumps([current])
                    + ";const oc=args=>{if(args[1]==='list')return JSON.stringify({jobs:rows});if(args[1]==='add'){rows.push({...rows[0],id:'duplicate',declarationKey:args[args.indexOf('--declaration-key')+1]});return '{}';}throw Error('unexpected mutation');};"
                )
                result = subprocess.run(
                    [
                        "node",
                        "-e",
                        script
                        + "(async()=>{"
                        + body
                        + "})().then(result=>console.log(JSON.stringify({result,count:rows.length})));",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    env={
                        **os.environ,
                        "NODE_OPTIONS": "",
                        "OPENCLAW_CONFIG_PATH": directory + "/openclaw.json",
                        "NBHD_INTERNAL_API_KEY": "test-key",
                    },
                )
                output = json.loads(result.stdout)
                self.assertEqual(output["count"], 1)
                return output["result"]

            with (
                patch("apps.orchestrator.azure_client._put_share_file", side_effect=write),
                patch("apps.orchestrator.azure_client.delete_workspace_file"),
                patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="test-key"),
                patch.object(op, "run_node", side_effect=execute),
            ):
                self.assertTrue(op.restore_crons(self.tenant, [desired])["verified"])
