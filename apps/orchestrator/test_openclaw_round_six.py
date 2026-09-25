"""Round six regressions: authority, authored instants, guards and private errors."""

import copy
import shutil
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from apps.cron.models import CronJob
from apps.cron.share_cron_sync import _desired_jobs
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator.migration_preservation import declaration_digest, preservation_reasons
from apps.orchestrator.runtime_guard import image_only_update_allowed, manual_version_update_allowed
from apps.orchestrator.test_tenant_openclaw_migration import job, record, tenant_fixture


class AuthorityTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(94940601)

    def test_complete_empty_export_quarantines_cache_before_promotion(self):
        self.tenant.postgres_cron_canonical = False
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        with suppress_cronjob_reconcile():
            row = CronJob.objects.create(tenant=self.tenant, name="cancelled", managed=True, data=job("cancelled"))
        rec = record()
        with patch.object(m, "live_source_jobs", return_value=[]):
            result = m.capture(self.tenant, rec)
        self.assertEqual(_desired_jobs(self.tenant), [])
        self.assertFalse(CronJob.objects.filter(pk=row.pk).exists())
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_migration["quarantined_cache"][0]["data"], row.data)
        self.assertEqual(self.tenant.openclaw_migration["quarantined_cache"][0]["gateway_job_id"], row.gateway_job_id)
        self.assertEqual(
            self.tenant.openclaw_migration["quarantined_cache"][0]["created_at"], row.created_at.isoformat()
        )
        self.assertEqual(result["quarantined_cache"], 1)
        self.assertTrue(self.tenant.postgres_cron_canonical)

    def test_preparation_ignores_stale_state_and_preserves_rendered_digest(self):
        from apps.orchestrator.cron_reconcile import _row_to_cron_dict

        data = job(schedule={"kind": "at", "at": "2027-01-01T00:00:00Z"}, state={"nextRunAtMs": 1798765200000})
        with suppress_cronjob_reconcile():
            row = CronJob.objects.create(tenant=self.tenant, name=data["name"], managed=True, data=data)
        before = declaration_digest(_row_to_cron_dict(row))
        m.prepare_writer_declarations(self.tenant)
        row.refresh_from_db()
        self.assertEqual(row.data["schedule"]["at"], "2027-01-01T00:00:00.000Z")
        self.assertEqual(declaration_digest(_row_to_cron_dict(row)), before)


class GuardTests(SimpleTestCase):
    def tenant(self, **overrides):
        return SimpleNamespace(
            container_id="synthetic",
            container_image_tag="2026.5.28-a",
            openclaw_version="2026.5.28",
            openclaw_migration={},
            **overrides,
        )

    def test_unresolved_submission_refuses_both_guards(self):
        tenant = self.tenant()
        for status in ("FAILED", "RUNNING", "BLOCKED_UNSUPPORTED", ""):
            tenant.openclaw_migration = {"status": status, "image_submitted": True}
            self.assertFalse(image_only_update_allowed(tenant, "2026.5.28-b"))
            self.assertFalse(manual_version_update_allowed(tenant, "2026.5.28-b", "2026.5.28"))

    def test_live_family_overrules_stale_database(self):
        app = SimpleNamespace(
            template=SimpleNamespace(
                containers=[SimpleNamespace(name="openclaw", image="registry/nbhd-openclaw:2026.9.4-a")]
            )
        )
        with patch("apps.orchestrator.azure_client.get_container_client") as client:
            client.return_value.container_apps.get.return_value = app
            self.assertFalse(image_only_update_allowed(self.tenant(), "2026.5.28-b"))
            client.return_value.container_apps.get.assert_called_once()


class NormalizationTests(SimpleTestCase):
    def test_null_optional_controls_match_absence(self):
        baseline = job()
        for field in ("toolsAllow", "lightContext", "model", "fallbacks", "timeoutSeconds"):
            variant = copy.deepcopy(baseline)
            variant["payload"][field] = None
            self.assertEqual(declaration_digest(variant), declaration_digest(baseline), field)

    def test_unproven_control_combination_is_denied(self):
        variant = job(
            payload={
                "kind": "agentTurn",
                "message": "synthetic",
                "model": "unproven/model",
                "toolsAllow": ["unproven_tool"],
                "lightContext": True,
                "timeoutSeconds": 123,
            }
        )
        self.assertIn("unproven_shape", preservation_reasons(variant))

    def test_state_cannot_supply_missing_authored_instant(self):
        variant = job(schedule={"kind": "at"}, state={"nextRunAtMs": 1798761600000})
        self.assertTrue(preservation_reasons(variant))


class QuietGatewayTests(SimpleTestCase):
    def test_private_http_body_never_reaches_operational_logs_or_exception(self):
        tenant = SimpleNamespace(id="synthetic", container_fqdn="synthetic.invalid")
        response = Mock(status_code=400, text="PRIVATE_SENTINEL")
        with (
            patch("apps.cron.gateway_client._get_gateway_token", return_value="synthetic"),
            patch("apps.cron.gateway_client.requests.post", return_value=response),
            self.assertLogs("apps.cron.gateway_client") as logs,
            self.assertRaises(GatewayError) as raised,
        ):
            invoke_gateway_tool(tenant, "cron.list", {}, metadata_only=True)
        self.assertNotIn("PRIVATE_SENTINEL", str(raised.exception))
        self.assertNotIn("PRIVATE_SENTINEL", str(logs.output))

    def test_migration_export_requests_quiet_client(self):
        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}) as invoke:
            m.live_source_jobs(SimpleNamespace())
        self.assertTrue(invoke.call_args.kwargs.get("metadata_only"))


class ShapeEvidenceTests(SimpleTestCase):
    def test_manifest_entries_have_lossless_stable_real_writer_evidence(self):
        import json
        from pathlib import Path

        from apps.orchestrator.migration_preservation import declaration_shape

        root = Path(__file__).parent
        manifest = json.loads((root / "cron-proven-shapes.json").read_text())
        self.assertTrue(manifest["shapes"])
        for entry in manifest["shapes"]:
            self.assertTrue(entry["fixtures"])
            for evidence in entry["fixtures"]:
                case = json.loads((root / evidence).read_text())
                with self.subTest(case=case["case"]):
                    desired = case["declaration"]
                    current = next(
                        j for j in case["list"]["json"]["jobs"] if j.get("declarationKey") == desired["declarationKey"]
                    )
                    self.assertEqual(declaration_shape(desired), entry["shape"])
                    self.assertTrue(case["selected"])
                    self.assertTrue(case["writerSameCron"])
                    self.assertEqual(declaration_digest(current, pins={}), declaration_digest(desired))
                    self.assertEqual(len(case["stability"]), 2)
                    for poll in case["stability"]:
                        self.assertEqual(poll["reconciliation"], {"ok": True, "applied": 0, "removed": 0, "skipped": 0})
                        row = next(
                            j
                            for j in json.loads(poll["list"]["stdout"])["jobs"]
                            if j.get("declarationKey") == desired["declarationKey"]
                        )
                        self.assertEqual(row["id"], current["id"])
                        self.assertEqual(declaration_digest(row, pins={}), declaration_digest(desired))

    @skipUnless(shutil.which("node"), "Node required; exercised in the local contract gate")
    def test_shared_normalization_python_js_and_comparator_on_every_capture(self):
        import json
        import subprocess
        from pathlib import Path

        from apps.orchestrator.migration_preservation import declaration_shape, normalized_declaration

        root = Path(__file__).parent
        cases = [
            json.loads(p.read_text())
            for p in (root / "fixtures/openclaw_94_cron_contract/round_six").glob("*.json")
            if p.name != "runtime-metadata.json"
        ]
        self.assertTrue(cases)
        values = []
        for case in cases:
            values.extend([case["authored"], case["declaration"]])
            values.extend(
                j
                for j in case["list"]["json"]["jobs"]
                if j.get("declarationKey") == case["declaration"]["declarationKey"]
            )
        script = f"""
import fs from 'node:fs';
import {{normalizedDeclaration,declarationShape}} from {json.dumps((root / "migration_cron_digest.mjs").resolve().as_uri())};
const jobs=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify(jobs.map(j=>[normalizedDeclaration(j,{{}}),declarationShape(j,{{}})])));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            input=json.dumps(values),
            text=True,
            capture_output=True,
            check=True,
        )
        for value, actual in zip(values, json.loads(result.stdout), strict=True):
            self.assertEqual(actual, [normalized_declaration(value, pins={}), declaration_shape(value, pins={})])

    @skipUnless(shutil.which("node"), "Node required; exercised in the local contract gate")
    def test_every_accepted_capture_matches_actual_operator_with_shared_pins(self):
        import json
        from pathlib import Path

        from apps.orchestrator.migration_preservation import canonical_digests
        from apps.orchestrator.test_runtime_operator import OperatorCronAdapterTests

        root = Path(__file__).parent / "fixtures/openclaw_94_cron_contract/round_six"
        for path in root.glob("*.json"):
            case = json.loads(path.read_text())
            if "case" not in case or preservation_reasons(case["authored"]):
                continue
            desired = case["declaration"]
            current = next(
                j for j in case["list"]["json"]["jobs"] if j.get("declarationKey") == desired["declarationKey"]
            )
            with self.subTest(case=case["case"]):
                result = OperatorCronAdapterTests.run_adapter(
                    self,
                    case["list"]["json"]["jobs"],
                    [desired],
                    cleanup=False,
                    canonical=canonical_digests([desired]),
                    now_ms=current["createdAtMs"],
                )
                self.assertTrue(result["result"]["matches"][0]["match"])
                self.assertEqual(result["mutations"], [])
                self.assertEqual(result["result"]["legacy"], [])

    def test_fixture_gate_refuses_new_control_values_and_combinations(self):
        from apps.orchestrator.migration_preservation import proven_shapes

        self.assertTrue(proven_shapes())
        for extra in (
            {"model": "future/model"},
            {"timeoutSeconds": 12345},
            {"toolsAllow": ["future_tool"]},
            {"fallbacks": ["future/model"]},
        ):
            self.assertIn(
                "unproven_shape",
                preservation_reasons(job(payload={"kind": "agentTurn", "message": "synthetic", **extra})),
            )


class ImportEvidenceTests(TestCase):
    def test_accepted_export_shapes_survive_complete_import_preparation_and_signed_selection(self):
        import json
        from pathlib import Path

        from apps.orchestrator.cron_reconcile import _row_to_cron_dict
        from apps.orchestrator.migration_preservation import writer_stable_declaration

        tenant = tenant_fixture(94940602)
        paths = (Path(__file__).parent / "fixtures/openclaw_94_cron_contract/round_six").glob("*.json")
        for path in paths:
            case = json.loads(path.read_text())
            if "case" not in case or preservation_reasons(case["authored"]):
                continue
            with self.subTest(case=case["case"]), suppress_cronjob_reconcile():
                CronJob.objects.filter(tenant=tenant).delete()
                tenant.postgres_cron_canonical = False
                tenant.save(update_fields=["postgres_cron_canonical"])
                source = case["authored"] | {"id": "synthetic-source"}
                with (
                    patch.object(m, "live_source_jobs", return_value=[source]),
                    patch.object(m, "assert_cutover_safe"),
                    patch("time.time", return_value=1780000000),
                ):
                    m.capture(tenant, record())
                    signed = _desired_jobs(tenant)
                self.assertEqual(len(signed), 1)
                self.assertEqual(declaration_digest(signed[0]), declaration_digest(source))
                self.assertEqual(signed[0]["schedule"], writer_stable_declaration(source)["schedule"])
                row = CronJob.objects.get(tenant=tenant)
                self.assertEqual(declaration_digest(_row_to_cron_dict(row)), declaration_digest(source))


class AdditionalSafetyTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(94940603)

    def test_noncanonical_live_export_replaces_conflicting_cache_and_quarantines_unsupported_cache_only(self):
        self.tenant.postgres_cron_canonical = False
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        with suppress_cronjob_reconcile():
            CronJob.objects.create(
                tenant=self.tenant, name="live", data=job("live", payload={"kind": "agentTurn", "message": "stale"})
            )
            CronJob.objects.create(
                tenant=self.tenant,
                name="cancelled",
                data=job("cancelled", payload={"kind": "systemEvent", "text": "stale"}),
            )
        live = job("live")
        with patch.object(m, "live_source_jobs", return_value=[live]):
            self.assertEqual(m.report_tenant(self.tenant)["quarantined_cache"], 1)
            rec = record()
            self.assertEqual(m.capture(self.tenant, rec)["quarantined_cache"], 1)
            # A retry never reintroduces the quarantined declaration.
            self.assertEqual(m.capture(self.tenant, rec)["quarantined_cache"], 1)
        self.assertEqual([j["name"] for j in _desired_jobs(self.tenant)], ["live"])
        self.assertEqual(CronJob.objects.get(tenant=self.tenant).data["payload"], live["payload"])

    def test_preparation_semantic_assertion_rolls_back_all_changes(self):
        with suppress_cronjob_reconcile():
            row = CronJob.objects.create(tenant=self.tenant, name="authored", data=job("authored"))
        with (
            patch(
                "apps.orchestrator.migration_preservation.writer_stable_declaration",
                return_value=job("authored", payload={"kind": "agentTurn", "message": "changed"}),
            ),
            self.assertRaisesRegex(m.MigrationError, "preparation_semantic_change"),
        ):
            m.prepare_writer_declarations(self.tenant)
        row.refresh_from_db()
        self.assertEqual(row.data, job("authored"))

    def test_failed_preparation_rolls_back_quarantine_and_promotion(self):
        self.tenant.postgres_cron_canonical = False
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        with suppress_cronjob_reconcile():
            row = CronJob.objects.create(tenant=self.tenant, name="cancelled", data=job("cancelled"))
        with (
            patch.object(m, "live_source_jobs", return_value=[]),
            patch.object(m, "prepare_writer_declarations", side_effect=m.MigrationError("synthetic_failure")),
            self.assertRaises(m.MigrationError),
        ):
            m.capture(self.tenant, record())
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.postgres_cron_canonical)
        self.assertTrue(CronJob.objects.filter(pk=row.pk).exists())
        self.assertNotIn("quarantined_cache", self.tenant.openclaw_migration)


class PrivacyAndGuardBoundaryTests(SimpleTestCase):
    def test_quiet_gateway_sanitizes_transport_and_ok_false_envelope(self):
        import requests

        tenant = SimpleNamespace(id="synthetic", container_fqdn="synthetic.invalid")
        for exception in (requests.Timeout("PRIVATE_SENTINEL"), requests.RequestException("PRIVATE_SENTINEL")):
            with (
                patch("apps.cron.gateway_client._get_gateway_token", return_value="synthetic"),
                patch("apps.cron.gateway_client.requests.post", side_effect=exception),
                self.assertRaises(GatewayError) as raised,
            ):
                invoke_gateway_tool(tenant, "cron.list", {}, metadata_only=True)
            self.assertNotIn("PRIVATE_SENTINEL", str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)
        response = Mock(status_code=200)
        response.json.return_value = {"ok": False, "error": "PRIVATE_SENTINEL"}
        with (
            patch("apps.cron.gateway_client._get_gateway_token", return_value="synthetic"),
            patch("apps.cron.gateway_client.requests.post", return_value=response),
            self.assertRaises(GatewayError) as raised,
        ):
            invoke_gateway_tool(tenant, "cron.list", {}, metadata_only=True)
        self.assertNotIn("PRIVATE_SENTINEL", str(raised.exception))

    def test_canary_partial_submission_is_refused_before_azure_update(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        tenant = SimpleNamespace(
            container_id="synthetic",
            container_image_tag="2026.5.28-a",
            openclaw_version="2026.5.28",
            openclaw_migration={"status": "FAILED", "image_submitted": True},
        )
        with (
            patch("apps.orchestrator.management.commands.canary_tenant_image.is_mock", return_value=False),
            patch("apps.orchestrator.management.commands.canary_tenant_image.Tenant.objects.filter") as rows,
            patch("apps.orchestrator.management.commands.canary_tenant_image.update_container_image") as update,
            self.settings(AZURE_ACR_SERVER="synthetic.invalid"),
            self.assertRaises(CommandError),
        ):
            rows.return_value.__getitem__.return_value = [tenant]
            call_command("canary_tenant_image", container="synthetic", tag="2026.5.28-b", stdout=StringIO())
        update.assert_not_called()

    def test_ambiguous_or_unavailable_live_identity_refuses(self):
        tenant = SimpleNamespace(
            container_id="synthetic",
            container_image_tag="2026.5.28-a",
            openclaw_version="2026.5.28",
            openclaw_migration={},
        )
        with patch("apps.orchestrator.azure_client.get_container_client") as client:
            client.return_value.container_apps.get.side_effect = RuntimeError("PRIVATE_SENTINEL")
            self.assertFalse(image_only_update_allowed(tenant, "2026.5.28-b"))
            client.return_value.container_apps.get.side_effect = None
            for image in (
                "registry/nbhd-openclaw:bare-sha",
                "registry/nbhd-openclaw@sha256:abcd",
                "registry/other:2026.5.28-a",
            ):
                client.return_value.container_apps.get.return_value = SimpleNamespace(
                    template=SimpleNamespace(containers=[SimpleNamespace(name="openclaw", image=image)])
                )
                self.assertFalse(image_only_update_allowed(tenant, "2026.5.28-b"))

    def test_conflicting_authored_instants_and_text_are_refused(self):
        self.assertTrue(
            preservation_reasons(job(schedule={"kind": "at", "at": "2027-01-01T00:00:00Z", "atMs": 1798765200000}))
        )
        self.assertIn(
            "conflicting_text_aliases",
            preservation_reasons(job(payload={"kind": "agentTurn", "message": "one", "text": "two"})),
        )

    def test_canary_sdk_failure_is_metadata_only(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        tenant = SimpleNamespace(
            container_id="synthetic",
            container_image_tag="2026.5.28-a",
            openclaw_version="2026.5.28",
            openclaw_migration={},
        )
        with (
            patch("apps.orchestrator.management.commands.canary_tenant_image.is_mock", return_value=False),
            patch("apps.orchestrator.management.commands.canary_tenant_image.Tenant.objects.filter") as rows,
            patch("apps.orchestrator.azure_client.get_container_client") as client,
            patch(
                "apps.orchestrator.management.commands.canary_tenant_image.update_container_image",
                side_effect=RuntimeError("PRIVATE_SENTINEL"),
            ),
            self.settings(AZURE_ACR_SERVER="synthetic.invalid"),
            self.assertRaises(CommandError) as raised,
        ):
            rows.return_value.__getitem__.return_value = [tenant]
            client.return_value.container_apps.get.return_value = SimpleNamespace(
                template=SimpleNamespace(
                    containers=[SimpleNamespace(name="openclaw", image="synthetic.invalid/nbhd-openclaw:2026.5.28-a")]
                )
            )
            call_command("canary_tenant_image", container="synthetic", tag="2026.5.28-b", stdout=StringIO())
        self.assertEqual(str(raised.exception), "canary_image_update_failed")
        self.assertTrue(raised.exception.__suppress_context__)

    def test_strict_config_envelope_does_not_log_private_section_errors(self):
        from apps.orchestrator import workspace_envelope as envelope

        section = SimpleNamespace(
            key="synthetic", enabled=lambda t: True, render=Mock(side_effect=RuntimeError("PRIVATE_SENTINEL"))
        )
        with (
            patch.object(envelope, "all_sections", return_value=[section]),
            patch.object(envelope, "_render_current_time_line", return_value="synthetic"),
            patch.object(envelope, "_persist_session_entities"),
            patch("apps.pii.redactor.RedactionSession"),
            self.assertLogs(envelope.logger) as logs,
        ):
            envelope.render_managed_region(SimpleNamespace(id="synthetic"), metadata_only=True)
        self.assertNotIn("PRIVATE_SENTINEL", str(logs.output))
        self.assertIn("section_render_failed", str(logs.output))

    def test_shape_variables_cannot_be_spoofed_with_shape_marker_objects(self):
        for variant in (
            job(description={"valueType": "string"}),
            job(payload={"kind": "agentTurn", "message": {"valueType": "string"}}),
        ):
            with self.subTest(variant=variant):
                self.assertTrue(preservation_reasons(variant))
