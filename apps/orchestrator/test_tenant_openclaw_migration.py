"""Safety contracts for the explicit 9.4 migration (all transports mocked)."""

import copy
import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.cron.models import CronJob
from apps.cron.share_cron_sync import _desired_jobs
from apps.orchestrator import openclaw_migration as migration
from apps.orchestrator.runtime_guard import image_only_update_allowed
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

TAG = "2026.9.4-abcdef0"
DIGEST = "sha256:" + "a" * 64


def job(name="reminder", **extra):
    return {
        "id": name + "-id",
        "name": name,
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
        "payload": {"kind": "agentTurn", "message": "Private reminder payload"},
        **extra,
    }


def tenant_fixture(chat_id=9494):
    tenant = create_tenant(display_name="Migration", telegram_chat_id=chat_id)
    tenant.status = Tenant.Status.ACTIVE
    tenant.container_id = "oc-test"
    tenant.container_fqdn = "test.invalid"
    tenant.openclaw_version = "2026.5.28"
    tenant.container_image_tag = "2026.5.28-abcdef0"
    tenant.save()
    return tenant


def record():
    return {"tag": TAG, "completed": [], "evidence": {}}


class MigrationOrderingTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture()

    def handlers(self, fail=None):
        calls = []

        def step(name):
            def run(tenant, rec):
                calls.append(name)
                tenant.refresh_from_db()
                self.assertEqual(tenant.openclaw_migration["step"], name)
                self.assertEqual(tenant.openclaw_migration["status"], "RUNNING")
                if name == fail:
                    raise migration.MigrationError("test failure")
                return {"ok": True}

            return run

        return {s: step(s) for s in migration.STEPS}, calls

    def test_strict_order_and_noop_after_pass(self):
        handlers, calls = self.handlers()
        with (
            patch.dict(migration.HANDLERS, handlers),
            patch.object(migration, "verify_existing", return_value={"status": "NOOP"}) as verify_existing,
        ):
            self.assertEqual(migration.migrate_tenant(self.tenant.pk, TAG)["status"], "PASS")
            self.assertEqual(migration.migrate_tenant(self.tenant.pk, TAG)["status"], "NOOP")
        verify_existing.assert_called_once()
        self.assertEqual(calls, list(migration.STEPS))
        self.tenant.refresh_from_db()
        self.assertEqual(set(self.tenant.openclaw_migration["step_completed_at"]), set(migration.STEPS))

    def test_every_failure_stops_and_resumes_only_uncompleted_steps(self):
        for index, failure in enumerate(migration.STEPS):
            with self.subTest(step=failure):
                Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration={})
                handlers, calls = self.handlers(fail=failure)
                with (
                    patch.dict(migration.HANDLERS, handlers),
                    self.assertRaisesRegex(migration.MigrationError, "no automatic rollback"),
                ):
                    migration.migrate_tenant(self.tenant.pk, TAG)
                self.assertEqual(calls, list(migration.STEPS[: index + 1]))
                self.tenant.refresh_from_db()
                self.assertEqual(self.tenant.openclaw_migration["status"], "FAILED")
                self.assertEqual(self.tenant.openclaw_migration["failure"]["step"], failure)
                handlers, calls = self.handlers()
                with patch.dict(migration.HANDLERS, handlers):
                    migration.migrate_tenant(self.tenant.pk, TAG)
                self.assertEqual(calls, list(migration.STEPS[index:]))

    def test_dry_run_has_no_writes_or_transport(self):
        handlers = {s: Mock() for s in migration.STEPS}
        out = StringIO()
        with patch.dict(migration.HANDLERS, handlers):
            call_command("migrate_tenant_openclaw", tenant=str(self.tenant.pk), tag=TAG, dry_run=True, stdout=out)
        self.assertIn("DRY_RUN", out.getvalue())
        self.assertIn("preflight,capture,image,version,config,crons,verify", out.getvalue())
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_migration, {})
        self.assertTrue(all(not h.called for h in handlers.values()))

    def test_hibernated_refused_before_transport(self):
        self.tenant.hibernated_at = timezone.now()
        self.tenant.save()
        with (
            patch.dict(migration.HANDLERS, {s: Mock() for s in migration.STEPS}),
            self.assertRaisesRegex(migration.MigrationError, "Hibernated"),
        ):
            migration.migrate_tenant(self.tenant.pk, TAG)

    def test_already_94_noop(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_version=migration.VERSION, container_image_tag=TAG)
        with patch.object(migration, "verify_existing", return_value={"status": "NOOP"}) as check:
            self.assertEqual(migration.migrate_tenant(self.tenant.pk, TAG)["status"], "NOOP")
        check.assert_called_once()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_migration, {})

    def test_already_94_dry_run_does_not_verify(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_version=migration.VERSION, container_image_tag=TAG)
        with patch.object(migration, "verify_existing") as check:
            self.assertEqual(migration.migrate_tenant(self.tenant.pk, TAG, dry_run=True)["status"], "DRY_RUN")
        check.assert_not_called()

    @override_settings(AZURE_ACR_SERVER="registry.invalid")
    def test_already_94_read_only_verification(self):
        self.tenant.openclaw_version = migration.VERSION
        self.tenant.container_image_tag = TAG
        image = f"registry.invalid/nbhd-openclaw:{TAG}"
        app = SimpleNamespace(template=SimpleNamespace(containers=[SimpleNamespace(name="openclaw", image=image)]))
        with (
            patch.object(migration, "get_app", return_value=app),
            patch.object(migration.runtime_operator, "config_observed") as config,
            patch.object(migration, "verify", return_value={"result": "PASS"}) as verify,
            patch.object(migration.azure_client, "update_container_image") as update,
        ):
            result = migration.verify_existing(self.tenant, {})
        self.assertEqual(result["verification"]["result"], "PASS")
        self.assertIn("Already on 9.4", result["note"])
        config.assert_called_once_with(self.tenant)
        self.assertEqual(verify.call_args.args[1]["evidence"]["preflight"]["target_image"], image)
        update.assert_not_called()

    def test_already_94_verification_failure_stops_command(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_version=migration.VERSION, container_image_tag=TAG)
        second = tenant_fixture(9495)
        with (
            patch.object(migration, "get_app", side_effect=RuntimeError("private sdk detail")),
            patch.dict(migration.HANDLERS, {s: Mock() for s in migration.STEPS}),
            self.assertRaisesRegex(CommandError, "verification_unavailable") as error,
        ):
            call_command("migrate_tenant_openclaw", tenants=f"{self.tenant.pk},{second.pk}", tag=TAG)
        self.assertNotIn("private sdk detail", str(error.exception))
        second.refresh_from_db()
        self.assertEqual(second.openclaw_migration, {})

    def test_same_tag_partial_record_is_resumed(self):
        rec = record() | {"status": "FAILED", "completed": ["preflight", "capture", "image", "version"]}
        Tenant.objects.filter(pk=self.tenant.pk).update(
            openclaw_version=migration.VERSION, container_image_tag=TAG, openclaw_migration=rec
        )
        handlers, calls = self.handlers()
        with patch.dict(migration.HANDLERS, handlers):
            migration.migrate_tenant(self.tenant.pk, TAG)
        self.assertEqual(calls, ["config", "crons", "verify"])

    def test_active_lease_rejected_and_expired_lease_resumed(self):
        rec = record() | {"status": "RUNNING", "lease_until": (timezone.now() + timedelta(minutes=10)).isoformat()}
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration=rec)
        with self.assertRaisesRegex(migration.MigrationError, "already running"):
            migration.migrate_tenant(self.tenant.pk, TAG)
        rec["lease_until"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_migration=rec)
        handlers, calls = self.handlers()
        with patch.dict(migration.HANDLERS, handlers):
            migration.migrate_tenant(self.tenant.pk, TAG)
        self.assertEqual(calls, list(migration.STEPS))

    def test_batch_stops_on_first_failure(self):
        second = tenant_fixture(9495)
        with (
            patch(
                "apps.orchestrator.management.commands.migrate_tenant_openclaw.migrate_tenant",
                side_effect=migration.MigrationError("stop"),
            ) as migrate,
            self.assertRaises(CommandError),
        ):
            call_command("migrate_tenant_openclaw", tenants=f"{self.tenant.pk},{second.pk}", tag=TAG)
        self.assertEqual(migrate.call_count, 1)

    def test_batch_validates_all_ids_before_start(self):
        with (
            patch("apps.orchestrator.management.commands.migrate_tenant_openclaw.migrate_tenant") as migrate,
            self.assertRaises(CommandError),
        ):
            call_command("migrate_tenant_openclaw", tenants=f"{self.tenant.pk},bad-id", tag=TAG)
        migrate.assert_not_called()

    def test_failure_does_not_persist_sdk_error_contents(self):
        with (
            patch.dict(migration.HANDLERS, {"preflight": Mock(side_effect=ValueError("secret-token"))}),
            self.assertRaises(migration.MigrationError) as error,
        ):
            migration.migrate_tenant(self.tenant.pk, TAG)
        self.tenant.refresh_from_db()
        self.assertNotIn("secret-token", str(error.exception))
        self.assertNotIn("secret-token", json.dumps(self.tenant.openclaw_migration))


class MigrationStepTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture()

    def test_capture_all_jobs_preserves_db_only_and_canonical_values(self):
        existing = CronJob.objects.create(
            tenant=self.tenant, name="reminder", data=job(payload={"kind": "agentTurn", "message": "Postgres wins"})
        )
        CronJob.objects.create(tenant=self.tenant, name="db-only", data=job("db-only"))
        unmanaged = CronJob.objects.create(tenant=self.tenant, name="unmanaged", managed=False, data=job("unmanaged"))
        jobs = [job(), job("disabled", enabled=False), job("_sync:agent"), job("unmanaged")]
        rec = record()
        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"details": {"jobs": jobs}}) as http:
            evidence = migration.capture(self.tenant, rec)
            migration.capture(self.tenant, rec)
        self.assertEqual(http.call_count, 2)
        self.assertEqual(rec["cron_export"], jobs)
        self.assertEqual(evidence["removed"], 0)
        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name="db-only").exists())
        existing.refresh_from_db()
        self.assertEqual(existing.data["payload"]["message"], "Postgres wins")
        self.assertFalse(CronJob.objects.get(tenant=self.tenant, name="disabled").enabled)
        self.assertEqual(rec["needs_agent_resync"], ["_sync:agent-id"])
        self.assertIn(str(unmanaged.pk), rec["preserved_unmanaged_ids"])
        desired = {j["name"] for j in _desired_jobs(self.tenant)}
        self.assertIn("unmanaged", desired)
        self.assertNotIn("disabled", desired)
        self.assertNotIn("_sync:agent", desired)

    def test_capture_uncertain_or_truncated_aborts_without_import(self):
        for response in ({}, {"jobs": [job()], "hasMore": True}, {"jobs": [job(), job()]}, {"jobs": [{}]}):
            with (
                self.subTest(response=response),
                patch("apps.cron.gateway_client.invoke_gateway_tool", return_value=response),
                self.assertRaisesRegex(migration.MigrationError, "provenance uncertain"),
            ):
                migration.capture(self.tenant, record())
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())

    def test_live_capture_failure_never_uses_snapshot(self):
        self.tenant.cron_jobs_snapshot = {"jobs": [job()]}
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", side_effect=RuntimeError("unreachable")),
            self.assertRaises(RuntimeError),
        ):
            migration.capture(self.tenant, record())
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())

    def test_preflight_snapshots_digest_and_redacts_literal_env(self):
        app = SimpleNamespace(template=Mock())
        app.template.containers = [SimpleNamespace(name="openclaw", image="registry.azurecr.io/nbhd-openclaw:abcdef0")]
        app.template.as_dict.return_value = {
            "containers": [{"env": [{"name": "TOKEN", "value": "private"}, {"name": "KEY", "secret_ref": "kv-ref"}]}]
        }
        with (
            patch.object(migration.azure_client, "is_mock", return_value=False),
            patch.object(migration, "get_app", return_value=app),
            patch.object(migration, "registry_digest", return_value=DIGEST),
        ):
            evidence = migration.preflight(self.tenant, record())
        self.assertEqual(evidence["source"]["version"], "2026.5.28")
        self.assertEqual(evidence["source"]["digest"], DIGEST)
        self.assertNotIn("private", json.dumps(evidence))
        self.assertIn("kv-ref", json.dumps(evidence))

    def test_preflight_registry_failure_stops_before_capture(self):
        capture_handler = Mock()
        with (
            patch.object(migration.azure_client, "is_mock", return_value=False),
            patch.object(migration, "get_app"),
            patch.object(migration, "registry_digest", side_effect=migration.MigrationError("ACR missing")),
            patch.dict(migration.HANDLERS, {"capture": capture_handler}),
            self.assertRaisesRegex(migration.MigrationError, "ACR missing"),
        ):
            migration.migrate_tenant(self.tenant.pk, TAG)
        capture_handler.assert_not_called()

    def test_image_resume_reuses_existing_revision(self):
        rec = record()
        rec["evidence"]["preflight"] = {"target_image": "image", "revision_suffix": "m94-existing"}
        app = SimpleNamespace(
            template=SimpleNamespace(
                containers=[SimpleNamespace(name="openclaw", image="image")], revision_suffix="m94-existing"
            )
        )
        with (
            patch.object(migration, "get_app", return_value=app),
            patch.object(migration.azure_client, "update_container_image") as update,
            patch.object(migration, "wait_healthy", return_value={}) as health,
        ):
            migration.image_step(self.tenant, rec)
        update.assert_not_called()
        health.assert_called_once()

    def test_image_submits_saved_suffix_and_waits(self):
        rec = record()
        rec["evidence"]["preflight"] = {"target_image": "image", "revision_suffix": "m94-fresh"}
        app = SimpleNamespace(
            template=SimpleNamespace(containers=[SimpleNamespace(name="openclaw", image="old")], revision_suffix="old")
        )
        calls = []
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}),
            patch.object(migration, "get_app", return_value=app),
            patch.object(
                migration.azure_client, "update_container_image", side_effect=lambda *a, **k: calls.append("image")
            ) as update,
            patch.object(migration, "wait_healthy", side_effect=lambda *a, **k: calls.append("health")),
        ):
            migration.image_step(self.tenant, rec)
        self.assertEqual(calls, ["image", "health"])
        update.assert_called_once_with(
            self.tenant.container_id, "image", revision_suffix="m94-fresh", operation_timeout=300, retrofit_storage=True
        )

    def test_version_and_tag_written_together(self):
        migration.version_step(self.tenant, record())
        self.tenant.refresh_from_db()
        self.assertEqual((self.tenant.container_image_tag, self.tenant.openclaw_version), (TAG, migration.VERSION))

    def test_config_failure_does_not_stamp(self):
        with (
            patch("apps.orchestrator.services.update_tenant_config", side_effect=RuntimeError("workspace failure")),
            self.assertRaises(RuntimeError),
        ):
            migration.config_step(self.tenant, record())
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 0)

    def test_config_strict_write_stamp_and_observe(self):
        with (
            patch("apps.orchestrator.services.update_tenant_config") as update,
            patch.object(migration.runtime_operator, "config_observed", return_value={"valid": True}),
            patch.object(migration, "wait_healthy"),
            patch.object(migration.time, "sleep"),
        ):
            result = migration.config_step(self.tenant, record())
        update.assert_called_once_with(str(self.tenant.pk), strict=True, refresh_crons=False)
        self.assertEqual(result["config_version"], 1)

    def test_config_stamp_does_not_consume_concurrent_pending_bump(self):
        self.tenant.config_version = 1
        self.tenant.pending_config_version = 2
        self.tenant.save()

        def concurrent_change(*args, **kwargs):
            Tenant.objects.filter(pk=self.tenant.pk).update(pending_config_version=3)

        with (
            patch("apps.orchestrator.services.update_tenant_config", side_effect=concurrent_change),
            patch.object(migration.runtime_operator, "config_observed", return_value={"valid": True}),
            patch.object(migration, "wait_healthy"),
            patch.object(migration.time, "sleep"),
        ):
            migration.config_step(self.tenant, record())
        self.tenant.refresh_from_db()
        self.assertEqual((self.tenant.config_version, self.tenant.pending_config_version), (2, 3))

    def test_verify_requires_matching_set_and_stable_ids(self):
        rec = record()
        rec["evidence"]["preflight"] = {"target_image": "image"}
        good = {"extras": [], "matches": [{"key": "nbhd:1", "id": "job1", "match": True}], "expected": 1}
        with (
            patch.object(migration, "_desired_jobs", return_value=[{"declarationKey": "nbhd:1"}]),
            patch.object(migration.runtime_operator, "inspect_signed_crons", return_value=good),
            patch.object(migration.runtime_operator, "console_error_counts", return_value={"errors": {"sqlite": 0}}),
            patch.object(migration, "wait_healthy", return_value={}),
            patch.object(migration.time, "sleep") as sleep,
        ):
            self.assertEqual(migration.verify(self.tenant, rec)["result"], "PASS")
            sleep.assert_called_once_with(25)
        changed = copy.deepcopy(good)
        changed["matches"][0]["id"] = "job2"
        with (
            patch.object(migration, "_desired_jobs", return_value=[{"declarationKey": "nbhd:1"}]),
            patch.object(migration.runtime_operator, "inspect_signed_crons", side_effect=[good, changed]),
            patch.object(migration.time, "sleep"),
            self.assertRaisesRegex(migration.MigrationError, "IDs changed"),
        ):
            migration.verify(self.tenant, rec)

    def test_verify_records_log_failures(self):
        rec = record()
        rec["evidence"]["preflight"] = {"target_image": "image"}
        good = {"extras": [], "matches": [], "expected": 0}
        with (
            patch.object(migration, "_desired_jobs", return_value=[]),
            patch.object(migration.runtime_operator, "inspect_signed_crons", return_value=good),
            patch.object(migration.runtime_operator, "console_error_counts", return_value={"errors": {"sqlite": 1}}),
            patch.object(migration, "wait_healthy"),
            patch.object(migration.time, "sleep"),
            self.assertRaisesRegex(migration.MigrationError, "console logs"),
        ):
            migration.verify(self.tenant, rec)
        self.assertEqual(rec["verification_failure"]["console"]["errors"]["sqlite"], 1)


class RuntimeGuardTests(SimpleTestCase):
    def test_refuse_cross_family_and_unknown_target(self):
        tenant = SimpleNamespace(
            openclaw_version="2026.5.28", container_image_tag="2026.5.28-abc1234", openclaw_migration={}
        )
        self.assertFalse(image_only_update_allowed(tenant, TAG))
        self.assertFalse(image_only_update_allowed(tenant, "abcdef0"))
        self.assertTrue(image_only_update_allowed(tenant, "2026.5.28-abcdef0"))
        tenant.openclaw_version = migration.VERSION
        self.assertFalse(image_only_update_allowed(tenant, "2026.5.28-abcdef0"))
        tenant.container_image_tag = TAG
        self.assertTrue(image_only_update_allowed(tenant, TAG))
        tenant.container_image_tag = "abcdef0"
        self.assertFalse(image_only_update_allowed(tenant, TAG))

    def test_registry_digest_validation(self):
        with (
            patch("azure.identity.ManagedIdentityCredential", side_effect=RuntimeError("private-token")),
            self.assertRaisesRegex(migration.MigrationError, "acr_digest_unavailable") as error,
        ):
            migration.registry_digest("test.azurecr.io/nbhd-openclaw:" + TAG)
        self.assertNotIn("private-token", str(error.exception))


class LifecycleAndDispatchTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture()

    @override_settings(OPENCLAW_IMAGE_TAG=TAG, AZURE_ACR_SERVER="test.azurecr.io")
    def test_fleet_guard_refuses_before_any_azure_call_even_same_tag_partial(self):
        self.tenant.container_image_tag = TAG
        self.tenant.save()
        with (
            patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image") as update,
            self.assertRaisesRegex(CommandError, "migrate_tenant_openclaw"),
        ):
            call_command("bump_all_tenant_images")
        update.assert_not_called()

    @override_settings(OPENCLAW_IMAGE_TAG=TAG, AZURE_ACR_SERVER="test.azurecr.io", DEPLOY_SECRET="test-deploy-secret")
    def test_dispatch_propagates_major_refusal(self):
        from django.test import RequestFactory

        from apps.cron.views import rollout_byo_image_bump

        request = RequestFactory().post(
            "/", data="{}", content_type="application/json", HTTP_X_DEPLOY_SECRET="test-deploy-secret"
        )
        with patch("apps.orchestrator.management.commands.bump_all_tenant_images.update_container_image") as update:
            response = rollout_byo_image_bump(request)
        self.assertEqual(response.status_code, 500)
        self.assertIn("migrate_tenant_openclaw", response.content.decode())
        update.assert_not_called()

    @override_settings(OPENCLAW_IMAGE_TAG=TAG, OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS="")
    def test_legacy_wake_retains_main_empty_allowlist_behavior(self):
        from apps.orchestrator.hibernation import wake_hibernated_tenant

        self.tenant.hibernated_at = timezone.now()
        self.tenant.save()
        with (
            patch("apps.orchestrator.azure_client.update_container_image") as update,
            patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False),
            patch("apps.orchestrator.azure_client.wake_container_app"),
            patch("apps.cron.publish.publish_task"),
        ):
            self.assertTrue(wake_hibernated_tenant(self.tenant))
        update.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_version, "2026.5.28")
        self.assertEqual(self.tenant.container_image_tag, "2026.5.28-abcdef0")

    def modern(self):
        self.tenant.openclaw_version = migration.VERSION
        self.tenant.save()
