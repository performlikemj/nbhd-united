"""Contract tests backed by isolated, exact fleet-image CLI recordings."""

import json
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator.cron_declarations import supported_declaration
from apps.orchestrator.migration_preservation import declaration_digest, preservation_reasons
from apps.orchestrator.test_tenant_openclaw_migration import TAG, record, tenant_fixture

FIXTURES = Path(__file__).parent / "fixtures/openclaw_94_cron_contract"
ORDINARY = ("cron-tz", "cron-top-hour", "every", "at-offset", "at-utc", "delivery-default", "delivery-empty")


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def observed(case):
    return next(
        j for j in case["list"]["json"]["jobs"] if j.get("declarationKey") == case["declaration"]["declarationKey"]
    )


class RuntimeContractTests(SimpleTestCase):
    def test_real_ordinary_rows_are_supported_and_semantically_equal(self):
        for name in ORDINARY:
            with self.subTest(name=name):
                case = fixture(name)
                self.assertEqual(case["add"]["exitCode"], 0)
                current, desired = observed(case), case["declaration"]
                self.assertTrue(supported_declaration(current))
                self.assertEqual(preservation_reasons(desired), set())
                self.assertEqual(declaration_digest(current, pins={}), declaration_digest(desired))
                self.assertIn("configRevision", current)
                self.assertIn("effectiveAgentId", current)
                self.assertFalse(supported_declaration(current | {"futureDefinitionControl": True}))

    def test_only_the_observed_default_scheduled_tool_policy_is_accepted(self):
        current = observed(fixture("cron-tz"))
        self.assertEqual(current["scheduledToolPolicy"], {"version": 1, "mode": "trusted"})
        for policy in (
            {"version": 2, "mode": "trusted"},
            {"version": 1, "mode": "restricted"},
            {"version": 1, "mode": "trusted", "unknown": True},
        ):
            self.assertFalse(supported_declaration(current | {"scheduledToolPolicy": policy}))

    def test_runtime_owned_namespaces_are_proven_reserved(self):
        evidence = fixture("runtime-owned")
        self.assertEqual({p["key"] for p in evidence["probes"]}, {"heartbeat:main", "skill-collection-review:main"})
        for probe in evidence["probes"]:
            for action in ("remove", "claim"):
                self.assertEqual(probe[action]["exitCode"], 1)
                self.assertIn("system-owned", probe[action]["stdout"])

    def test_prepared_one_shots_are_stable_in_real_writer_on_two_passes(self):
        for name in ("at-offset-stable", "at-utc-stable"):
            case = fixture(name)
            self.assertTrue(case["writerSameCron"])
            current = observed(case)
            self.assertEqual(len(case["stability"]), 2)
            for poll in case["stability"]:
                self.assertEqual(poll["reconciliation"], {"ok": True, "applied": 0, "removed": 0, "skipped": 0})
                rows = json.loads(poll["list"]["stdout"])["jobs"]
                self.assertEqual(
                    next(j["id"] for j in rows if j.get("declarationKey") == current["declarationKey"]), current["id"]
                )

    @skipUnless(shutil.which("node"), "Node is required for real adapter execution")
    def test_operator_accepts_full_real_lists_without_mutating_system_owned_rows(self):
        from apps.orchestrator.migration_preservation import canonical_digests
        from apps.orchestrator.test_runtime_operator import OperatorCronAdapterTests

        for name in ORDINARY:
            case = fixture(name)
            result = OperatorCronAdapterTests.run_adapter(
                self,
                case["list"]["json"]["jobs"],
                [case["declaration"]],
                cleanup=True,
                canonical=canonical_digests([case["declaration"]]),
                now_ms=observed(case)["createdAtMs"],
            )
            self.assertTrue(result["result"]["matches"][0]["match"], name)
            self.assertEqual(result["result"]["legacy"], [])
            self.assertEqual(result["mutations"], [])
        case = fixture("cron-tz")
        unknown = observed(case) | {"id": "unknown", "declarationKey": "other:main"}
        result = OperatorCronAdapterTests.run_adapter(self, [unknown], [], cleanup=False)
        self.assertEqual(result["result"]["legacy"], ["unknown"])

    def test_actual_writer_losses_and_cli_rejections_are_blocked(self):
        for name in ("every-anchor", "delivery-explicit", "system-main", "system-isolated", "disabled"):
            with self.subTest(name=name):
                case = fixture(name)
                self.assertTrue(preservation_reasons(case["declaration"]))
                if name.startswith("system-"):
                    self.assertNotEqual(case["add"]["exitCode"], 0)
                else:
                    self.assertEqual(case["add"]["exitCode"], 0)
                    self.assertNotEqual(declaration_digest(observed(case)), declaration_digest(case["declaration"]))
                if name == "disabled":
                    self.assertFalse(case["selected"])
                    self.assertEqual(case["signedJobs"], [])

    @skipUnless(shutil.which("node"), "Node is required for real comparator execution")
    def test_js_comparator_and_python_digest_agree_on_real_rows(self):
        compare = (Path(__file__).parent / "migration_cron_compare.mjs").resolve().as_uri()
        digest = (Path(__file__).parent / "migration_cron_digest.mjs").resolve().as_uri()
        cases = [fixture(name) for name in ORDINARY]
        script = f"""
import {{sameCron,supportedDeclaration}} from {json.dumps(compare)};
import {{normalizedDeclaration,stableJSON}} from {json.dumps(digest)};
import {{createHash}} from 'node:crypto';
const cases={json.dumps(cases)};
console.log(JSON.stringify(cases.map(c=>{{
 const desired=c.declaration, current=c.list.json.jobs.find(j=>j.declarationKey===desired.declarationKey);
 const altered=structuredClone(current); altered.payload.message+=' changed';
 return {{same:sameCron(current,desired),changed:sameCron(altered,desired),
 unknown:sameCron({{...current,futureDefinitionControl:true}},desired),
 supported:supportedDeclaration(current),
 digest:createHash('sha256').update(stableJSON(normalizedDeclaration(current))).digest('hex')}};
}})));
"""
        process = subprocess.run(
            ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True
        )
        for case, result in zip(cases, json.loads(process.stdout), strict=True):
            with self.subTest(name=case["case"]):
                self.assertEqual(
                    result,
                    {
                        "same": True,
                        "changed": False,
                        "unknown": False,
                        "supported": True,
                        "digest": declaration_digest(case["declaration"]),
                    },
                )


class RuntimeMigrationTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(94940501)

    def row(self, declaration):
        with suppress_cronjob_reconcile():
            return CronJob.objects.create(tenant=self.tenant, name=declaration["name"], data=declaration, managed=True)

    def test_verify_only_ignores_generated_timing_only_with_matching_canonical(self):
        self.tenant.openclaw_version, self.tenant.container_image_tag = m.VERSION, TAG
        for name in ("cron-top-hour", "every"):
            case = fixture(name)
            row = self.row(case["declaration"])
            current = observed(case)
            current["declarationKey"] = f"nbhd:{row.pk}"
            with (
                patch.object(m.runtime_operator, "preservation_inventory", return_value=[current]),
                patch.object(m, "get_app"),
                patch.object(m, "_image", return_value=f"registry/nbhd-openclaw:{TAG}"),
                patch.object(m.runtime_operator, "config_observed"),
                patch.object(m, "verify", return_value={"result": "PASS"}) as verify,
                patch.object(m.settings, "AZURE_ACR_SERVER", "registry"),
                patch.object(m, "_save") as save,
            ):
                self.assertEqual(m.verify_existing(self.tenant, {})["status"], "NOOP")
                verify.assert_called_once()
                save.assert_not_called()
                # An unmatched observation has no evidence that timing was unpinned.
                current["declarationKey"] = "nbhd:unmatched"
                self.assertEqual(m.verify_existing(self.tenant, {})["status"], "BLOCKED_UNSUPPORTED")
            with suppress_cronjob_reconcile():
                row.delete()

    def test_system_event_precheck_refuses_before_capture_mutation(self):
        for name in ("system-main", "system-isolated"):
            case = fixture(name)
            source = case["declaration"] | {"id": "synthetic-source"}
            with (
                patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": [source]}),
                patch.object(m, "_save") as save,
                self.assertRaises(m.PreservationError),
            ):
                m.capture(self.tenant, record())
            save.assert_not_called()
            self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())

    def test_capture_prepares_one_shots_for_unchanged_writer_before_cutover(self):
        from apps.cron.share_cron_sync import _desired_jobs

        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        for name in ("at-offset", "at-utc"):
            declaration = fixture(name)["declaration"]
            row = self.row(declaration)
            with (
                patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}),
                patch.object(m, "assert_cutover_safe"),
            ):
                m.capture(self.tenant, record())
            row.refresh_from_db()
            self.assertEqual(row.data["schedule"], {"kind": "at", "at": "2027-01-01T00:00:00.000Z"})
            self.assertEqual(declaration_digest(row.data), declaration_digest(declaration))
            with patch("time.time", return_value=1780000000):
                signed = _desired_jobs(self.tenant)
            self.assertEqual(next(j for j in signed if j["name"] == row.name)["schedule"], row.data["schedule"])
