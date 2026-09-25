"""Round-seven boundary, crash/recovery and real-image boot contracts."""

import copy
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.cron.models import CronJob
from apps.cron.signals import suppress_cronjob_reconcile
from apps.orchestrator import openclaw_migration as m
from apps.orchestrator import personas, runtime_operator
from apps.orchestrator.cron_declarations import declaration_fields
from apps.orchestrator.migration_preservation import declaration_digest, json_type_key, preservation_reasons
from apps.orchestrator.runtime_guard import image_only_update_allowed
from apps.orchestrator.test_tenant_openclaw_migration import TAG, job, tenant_fixture

ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures/openclaw_94_cron_contract"


def fixture(name):
    return json.loads((FIXTURES / "round_six" / (name + ".json")).read_text())


def inventory_result(jobs, pins=None):
    with patch.object(runtime_operator, "run_node") as run:
        runtime_operator.preservation_inventory(None, canonical_pins=pins)
    body = run.call_args.args[1]
    script = "const crypto=require('node:crypto');const oc=()=>" + json.dumps(json.dumps({"jobs": jobs})) + ";"
    script += "(async()=>{" + body + "})().then(r=>console.log(JSON.stringify(r)));"
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class TypeBoundaries(SimpleTestCase):
    def test_boolean_integer_float_shapes_are_distinct(self):
        self.assertEqual(len({json_type_key(v) for v in (1, True, 1.0, 0, False, 0.0)}), 6)
        for field, part, values in (
            ("lightContext", "payload", (1, 1.0, 0, 0.0)),
            ("deleteAfterRun", None, (0, 0.0, 1, 1.0)),
            ("enabled", None, (1, 1.0)),
            ("sessionTarget", None, (False, 0, 0.0)),
            ("channel", "delivery", (False, 0, 0.0)),
            ("tz", "schedule", (False, 0, 0.0)),
            ("bestEffort", "delivery", (0, 0.0)),
        ):
            for value in values:
                declaration = copy.deepcopy(
                    fixture("control-lightContext" if field == "lightContext" else "cron-tz")["declaration"]
                )
                (declaration.setdefault(part, {}) if part else declaration)[field] = value
                with self.subTest(field=field, value=value, type=type(value)):
                    self.assertTrue(preservation_reasons(declaration))

    def test_generated_policy_requires_python_integer(self):
        for version in (True, False, 1.0, 0, 0.0, "1"):
            self.assertIn(
                "scheduledToolPolicy",
                declaration_fields(job(scheduledToolPolicy={"version": version, "mode": "trusted"})),
            )
        self.assertNotIn(
            "scheduledToolPolicy", declaration_fields(job(scheduledToolPolicy={"version": 1, "mode": "trusted"}))
        )

    @skipUnless(shutil.which("node"), "Node required; run in local contract gate")
    def test_js_boolean_numeric_boundaries_and_policy(self):
        for value in (1, True, 0, False, 1.0, 0.0):
            declaration = copy.deepcopy(fixture("control-lightContext")["declaration"])
            declaration["payload"]["lightContext"] = value
            with self.subTest(value=value, type=type(value)):
                self.assertEqual(bool(inventory_result([declaration])), type(value) is not bool)
        for value in (True, False, 0, "1", 1):
            declaration = fixture("cron-tz")["declaration"]
            declaration["scheduledToolPolicy"] = {"version": value, "mode": "trusted"}
            self.assertEqual(bool(inventory_result([declaration])), type(value) is not int or value != 1)


@skipUnless(shutil.which("node"), "Node required; run in local contract gate")
class ReplicaInventoryTests(SimpleTestCase):
    def test_exact_controls_checked_before_private_values_leave_replica(self):
        for name in ("control-model", "typed-pure_reminder-cron", "agent-main", "cron-tz"):
            case = fixture(name)
            observed = next(
                j
                for j in case["list"]["json"]["jobs"]
                if j.get("declarationKey") == case["declaration"]["declarationKey"]
            )
            self.assertEqual(inventory_result([observed]), [], name)
            observed["payload"]["model"] = "PRIVATE_UNPROVEN_MODEL"
            observed["payload"]["message"] = "PRIVATE_TEXT"
            observed["agentId"] = "PRIVATE_AGENT"
            observed["name"] = "PRIVATE_NAME"
            result = inventory_result([observed])
            self.assertIn("unproven_shape", result[0]["reasons"])
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertEqual(set(result[0]), {"key", "reasons"})

    def test_generated_timing_requires_matching_canonical_key_and_kind(self):
        for name in ("cron-top-hour", "every"):
            case = fixture(name)
            observed = next(
                j
                for j in case["list"]["json"]["jobs"]
                if j.get("declarationKey") == case["declaration"]["declarationKey"]
            )
            self.assertTrue(inventory_result([observed]))
            pins = {
                observed["declarationKey"]: {
                    "kind": observed["schedule"]["kind"],
                    "anchorMs": False,
                    "staggerMs": False,
                }
            }
            self.assertEqual(inventory_result([observed], pins), [])


class DigestGuardTests(SimpleTestCase):
    def test_tag_and_digest_acceptance_keeps_family_and_submission_guards(self):
        tenant = SimpleNamespace(
            container_id="synthetic",
            container_image_tag=TAG,
            openclaw_version=m.VERSION,
            openclaw_migration={"status": "PASS", "image_submitted": True},
        )
        live = SimpleNamespace(name="openclaw", image="")
        with patch("apps.orchestrator.azure_client.get_container_client") as client:
            client.return_value.container_apps.get.return_value.template.containers = [live]
            for suffix, accepted in (
                ("", True),
                ("@sha256:" + "a" * 64, True),
                ("@sha256:bad", False),
                ("@sha256:" + "a" * 65, False),
                ("@sha256:" + "a" * 64 + "@extra", False),
            ):
                live.image = "registry/nbhd-openclaw:" + TAG + suffix
                self.assertEqual(image_only_update_allowed(tenant, TAG), accepted)
            live.image = "registry/nbhd-openclaw:2026.5.28-abcdef0@sha256:" + "a" * 64
            self.assertFalse(image_only_update_allowed(tenant, TAG))
            live.image = "registry/nbhd-openclaw:" + TAG + "@sha256:" + "a" * 64
            tenant.openclaw_migration["status"] = "RUNNING"
            self.assertFalse(image_only_update_allowed(tenant, TAG))


class CheckpointFenceTests(SimpleTestCase):
    def test_stale_connection_retry_keeps_owner_fence_and_refuses_reclaimed_owner(self):
        from django.db import InterfaceError

        for updated in (0, 1):
            tenant = SimpleNamespace(pk="synthetic")
            record = {"owner_token": "old-owner"}
            with (
                patch.object(m.Tenant.objects, "filter") as query,
                patch("django.db.connection.close"),
                patch("apps.tenants.middleware.set_rls_context"),
            ):
                query.return_value.update.side_effect = [InterfaceError("stale"), updated]
                if updated:
                    m._save(tenant, record)
                else:
                    with self.assertRaisesRegex(m.MigrationError, "migration_owner_fenced"):
                        m._save(tenant, record)
                self.assertEqual(query.call_count, 2)
                for call in query.call_args_list:
                    self.assertEqual(call.kwargs, {"pk": "synthetic", "openclaw_migration__owner_token": "old-owner"})


class PrestagingTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(949407)
        self.files = {}
        self.enterContext(patch.object(m.time, "sleep"))
        for target, kwargs in (
            # Full-suite environment probes can unset AZURE_MOCK. Pin this
            # synthetic storage fixture and forbid credential acquisition.
            ("apps.orchestrator.azure_client.is_mock", {"return_value": True}),
            (
                "apps.orchestrator.storage_credentials.acquire_account_key",
                {"side_effect": AssertionError("unexpected live storage access")},
            ),
            ("apps.cron.gateway_client.get_gateway_token_for_tenant", {"return_value": "local-contract-token"}),
            (
                "apps.orchestrator.azure_client._put_share_file",
                {"side_effect": lambda tenant, path, *, data, **kw: self.files.update({path: data})},
            ),
            (
                "apps.orchestrator.azure_client.download_workspace_file_binary",
                {"side_effect": lambda tenant, path: self.files.get(path)},
            ),
        ):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_crash_after_image_apply_has_signed_checkpoint_and_resume_reuses_revision(self):
        source = job(schedule={"kind": "at", "at": (timezone.now() + timedelta(hours=3)).isoformat()})
        app = SimpleNamespace(
            template=SimpleNamespace(containers=[SimpleNamespace(name="openclaw", image="old")], revision_suffix="old")
        )
        target = "registry/nbhd-openclaw:" + TAG
        rec = {"target_image": target, "revision_suffix": "m94-saved"}
        stale = {}

        def apply(*args, **kwargs):
            self.tenant.refresh_from_db()
            stale.update(copy.deepcopy(self.tenant.openclaw_migration))
            self.assertEqual(stale["signed_prestaged"]["count"], 1)
            self.assertIn("recovery", stale)
            self.assertIn("nbhd-crons.json", self.files)
            self.assertEqual(
                stale["signed_prestaged"]["digest"],
                __import__("hashlib").sha256(self.files["nbhd-crons.json"]).hexdigest(),
            )
            self.assertEqual(self.tenant.openclaw_version, "2026.5.28")
            self.assertTrue(self.tenant.openclaw_migration_cron_fenced)
            from apps.orchestrator.migration_cron_fence import cron_edits_fenced

            with self.assertNumQueries(0):
                self.assertTrue(cron_edits_fenced(self.tenant))
            app.template.containers[0].image = target
            app.template.revision_suffix = "m94-saved"
            raise SystemExit("simulated process death after Azure apply")

        with (
            patch.object(m, "report_tenant", return_value={"status": "READY"}),
            patch.object(m, "live_source_jobs", return_value=[source]),
            patch.object(m, "get_app", return_value=app),
            patch.object(m, "wait_healthy", return_value={"health": True}),
            patch.object(m.azure_client, "update_container_image", side_effect=apply) as update,
            patch.dict(
                m.HANDLERS,
                {
                    "preflight": lambda *a: rec,
                    "config": lambda *a: {},
                    "crons": lambda *a: {},
                    "verify": lambda *a: {"result": "PASS"},
                },
            ),
        ):
            with self.assertRaises(SystemExit):
                m.migrate_tenant(self.tenant.pk, TAG)
            lease = __import__("django.utils.dateparse", fromlist=["parse_datetime"]).parse_datetime(
                stale["lease_until"]
            )
            self.assertLessEqual(lease - timezone.now(), timedelta(minutes=10))
            with self.assertRaisesRegex(m.MigrationError, "already running"):
                m.migrate_tenant(self.tenant.pk, TAG)
            with self.assertRaisesRegex(m.MigrationError, "dead_owner_token_mismatch"):
                m.migrate_tenant(self.tenant.pk, TAG, takeover="wrong", confirm_owner_dead=True)
            self.assertEqual(
                m.migrate_tenant(self.tenant.pk, TAG, takeover=stale["owner_token"], confirm_owner_dead=True)["status"],
                "PASS",
            )
            update.assert_called_once()
        with self.assertRaisesRegex(m.MigrationError, "migration_owner_fenced"):
            m._save(self.tenant, stale)
        with self.assertRaisesRegex(m.MigrationError, "migration_owner_fenced"):
            m.version_step(self.tenant, stale)

    def test_health_rechecks_canonical_content_and_file_bytes(self):
        with suppress_cronjob_reconcile():
            row = CronJob.objects.create(tenant=self.tenant, name="reminder", data=job(), managed=True)
        record = {}
        first = m.stage_signed_crons(self.tenant, record, checkpoint="signed_prestaged")
        row.data["payload"]["message"] = "Updated canonical message"
        CronJob.objects.filter(pk=row.pk).update(data=row.data)
        after = m.stage_signed_crons(self.tenant, record, checkpoint="signed_after_health")
        self.assertNotEqual(first["digest"], after["digest"])
        self.assertTrue(after["rewritten"])
        self.assertFalse(m.stage_signed_crons(self.tenant, record, checkpoint="signed_after_health")["rewritten"])
        self.files["nbhd-crons.json"] = b"corrupt"
        self.assertTrue(m.stage_signed_crons(self.tenant, record, checkpoint="signed_after_health")["rewritten"])

    def test_unverified_prestage_prevents_image_submission(self):
        app = SimpleNamespace(
            template=SimpleNamespace(containers=[SimpleNamespace(name="openclaw", image="old")], revision_suffix="old")
        )
        record = {"tag": TAG, "evidence": {"preflight": {"target_image": "new", "revision_suffix": "new"}}}
        with (
            patch.object(m, "live_source_jobs", return_value=[job()]),
            patch.object(m, "get_app", return_value=app),
            patch.object(m.azure_client, "download_workspace_file_binary", return_value=b"corrupt"),
            patch.object(m.azure_client, "update_container_image") as image,
            self.assertRaisesRegex(m.MigrationError, "signed_file_readback_mismatch"),
        ):
            m.image_step(self.tenant, record)
        image.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.openclaw_migration.get("image_submitted"))

    def test_sixty_minute_guard_checks_canonical_and_runtime(self):
        for minutes in (21, 59, 60):
            declaration = job(schedule={"kind": "at", "at": (timezone.now() + timedelta(minutes=minutes)).isoformat()})
            with self.assertRaisesRegex(m.MigrationError, "cron_imminent"):
                m.preservation_precheck(self.tenant, [declaration])
            self.tenant.postgres_cron_canonical = True
            with (
                patch.object(m, "canonical_inventory", return_value=[(declaration, True)]),
                self.assertRaisesRegex(m.MigrationError, "cron_imminent"),
            ):
                m.preservation_precheck(self.tenant, [])


class QuietPersonaTests(SimpleTestCase):
    @override_settings(AZURE_KV_SECRET_SOUL_MD="synthetic-soul", AZURE_KV_SECRET_AGENTS_MD="synthetic-agents")
    def test_workspace_quiet_reaches_cold_vault_loaders_and_sanitizes_outer_errors(self):
        for loader in (personas._load_soul_from_key_vault, personas._load_agents_md_from_key_vault):
            if hasattr(loader, "_cached"):
                del loader._cached
            self.addCleanup(lambda loader=loader: vars(loader).pop("_cached", None))
        with (
            patch("builtins.open", side_effect=FileNotFoundError),
            patch.dict(os_environ(), {"NBHD_SOUL_MD_TEMPLATE": "", "NBHD_AGENTS_MD_TEMPLATE": ""}),
            patch(
                "apps.orchestrator.azure_client.read_key_vault_secret", side_effect=RuntimeError("PRIVATE_SENTINEL")
            ) as read,
            self.assertLogs("apps.orchestrator.personas", level="WARNING") as logs,
        ):
            personas.render_workspace_files("default", metadata_only=True)
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(call.kwargs == {"metadata_only": True} for call in read.call_args_list))
        self.assertNotIn("PRIVATE_SENTINEL", str(logs.output))

    @override_settings(
        AZURE_KV_SECRET_SOUL_MD="synthetic-soul",
        AZURE_KV_SECRET_AGENTS_MD="synthetic-agents",
        AZURE_KEY_VAULT_NAME="synthetic",
    )
    def test_real_vault_helper_is_quiet_only_when_opted_in(self):
        for quiet in (True, False):
            for loader in (personas._load_soul_from_key_vault, personas._load_agents_md_from_key_vault):
                vars(loader).pop("_cached", None)
                self.addCleanup(lambda loader=loader: vars(loader).pop("_cached", None))
            with (
                patch("builtins.open", side_effect=FileNotFoundError),
                patch.dict(os_environ(), {"NBHD_SOUL_MD_TEMPLATE": "", "NBHD_AGENTS_MD_TEMPLATE": ""}),
                patch("apps.orchestrator.azure_client._is_mock", return_value=False),
                patch("apps.orchestrator.azure_client._get_provisioner_credential"),
                patch("azure.keyvault.secrets.SecretClient") as client,
                self.assertLogs("apps.orchestrator.azure_client", level="WARNING") as logs,
            ):
                client.return_value.get_secret.side_effect = RuntimeError("PRIVATE_SENTINEL")
                personas.render_workspace_files("default", **({"metadata_only": True} if quiet else {}))
            self.assertEqual("PRIVATE_SENTINEL" in str(logs.output), not quiet)


def os_environ():
    import os

    return os.environ


class StrictRenderingContextTests(TestCase):
    def test_strict_config_passes_opt_in_and_shared_default_omits_it(self):
        from apps.orchestrator import services

        tenant = tenant_fixture(94940702)
        for strict in (True, False):
            with (
                patch.object(services, "generate_openclaw_config", return_value={}),
                patch.object(services, "_audit_and_log"),
                patch.object(services, "upload_config_to_file_share"),
                patch.object(personas, "render_workspace_files", side_effect=SystemExit) as render,
                self.assertRaises(SystemExit),
            ):
                services.update_tenant_config(str(tenant.pk), strict=strict)
            self.assertEqual(render.call_args.kwargs.get("metadata_only", False), strict)
            if not strict:
                self.assertNotIn("metadata_only", render.call_args.kwargs)


class RealBootFixtureTests(SimpleTestCase):
    def test_real_entrypoint_installed_prestaged_jobs_without_manual_cron_calls(self):
        from hashlib import sha256

        capture = json.loads((FIXTURES / "prestaged-boot.json").read_text())
        self.assertTrue(capture["prestagedBeforeStart"])
        self.assertEqual(capture["manualCronMutations"], 0)
        self.assertEqual(capture["entrypoint"], ["/usr/local/bin/nbhd-openclaw-entrypoint"])
        root = ROOT.parent.parent
        for source, digest in capture["sources"].items():
            self.assertEqual(sha256((root / source).read_bytes()).hexdigest(), digest)
        for declaration in capture["declarations"]:
            self.assertFalse(preservation_reasons(declaration))
            for poll in capture["polls"]:
                found = next(j for j in poll["jobs"] if j.get("declarationKey") == declaration["declarationKey"])
                self.assertEqual(declaration_digest(found, pins={}), declaration_digest(declaration))
                self.assertEqual(found["id"], capture["stableIds"][declaration["declarationKey"]])
