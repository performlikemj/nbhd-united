"""Additional orchestrator service coverage."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.cron.gateway_client import GatewayError
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.orchestrator.services import deprovision_tenant, provision_tenant, seed_cron_jobs


class OrchestratorServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Orchestrator", telegram_chat_id=515151)

    @override_settings(OPENCLAW_CONTAINER_SECRET_BACKEND="keyvault")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch(
        "apps.orchestrator.services.create_managed_identity",
        return_value={"id": "/identities/1", "client_id": "client-1", "principal_id": "principal-1"},
    )
    @patch("apps.orchestrator.services.assign_key_vault_role")
    @patch("apps.orchestrator.services.assign_acr_pull_role")
    @patch(
        "apps.orchestrator.services.store_tenant_internal_key_in_key_vault",
        return_value="tenant-xxx-internal-key",
    )
    @patch("apps.orchestrator.services.seed_cron_jobs", return_value={"tenant_id": "seed", "jobs_total": 4, "created": 4, "errors": 0})
    @patch("apps.cron.views._schedule_qstash_task", create=True, return_value=None)
    @patch("apps.orchestrator.services.create_tenant_file_share")
    @patch("apps.orchestrator.services.register_environment_storage")
    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch(
        "apps.orchestrator.services.create_container_app",
        return_value={"name": "oc-tenant", "fqdn": "oc-tenant.internal.azurecontainerapps.io"},
    )
    def test_provision_happy_path(
        self,
        _mock_create_container,
        _mock_upload_config,
        _mock_register_storage,
        _mock_create_file_share,
        _mock_schedule_qstash,
        _mock_seed_cron_jobs,
        _mock_store_kv_key,
        _mock_assign_acr_role,
        _mock_assign_kv_role,
        _mock_create_identity,
        _mock_config_json,
        _mock_generate_config,
    ):
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        self.assertEqual(self.tenant.container_id, "oc-tenant")
        self.assertEqual(self.tenant.container_fqdn, "oc-tenant.internal.azurecontainerapps.io")
        self.assertEqual(self.tenant.managed_identity_id, "/identities/1")
        self.assertIsNotNone(self.tenant.provisioned_at)
        _mock_assign_kv_role.assert_called_once_with("principal-1")
        self.assertEqual(len(self.tenant.internal_api_key_hash), 64)
        self.assertIsNotNone(self.tenant.internal_api_key_set_at)
        _mock_store_kv_key.assert_called_once()
        _mock_create_container.assert_called_once()
        call_kwargs = _mock_create_container.call_args.kwargs
        self.assertEqual(call_kwargs["internal_api_key_kv_secret"], "tenant-xxx-internal-key")

    @override_settings(OPENCLAW_CONTAINER_SECRET_BACKEND="env")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch(
        "apps.orchestrator.services.create_managed_identity",
        return_value={"id": "/identities/10", "client_id": "client-10", "principal_id": "principal-10"},
    )
    @patch("apps.orchestrator.services.assign_key_vault_role")
    @patch("apps.orchestrator.services.assign_acr_pull_role")
    @patch(
        "apps.orchestrator.services.store_tenant_internal_key_in_key_vault",
        return_value="tenant-xxx-internal-key",
    )
    @patch("apps.orchestrator.services.seed_cron_jobs", return_value={"tenant_id": "seed", "jobs_total": 4, "created": 4, "errors": 0})
    @patch("apps.cron.views._schedule_qstash_task", create=True, return_value=None)
    @patch("apps.orchestrator.services.create_tenant_file_share")
    @patch("apps.orchestrator.services.register_environment_storage")
    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch(
        "apps.orchestrator.services.create_container_app",
        return_value={"name": "oc-tenant", "fqdn": "oc-tenant.internal.azurecontainerapps.io"},
    )
    def test_provision_skips_kv_role_assignment_for_env_backend(
        self,
        _mock_create_container,
        _mock_upload_config,
        _mock_register_storage,
        _mock_create_file_share,
        _mock_schedule_qstash,
        _mock_seed_cron_jobs,
        _mock_store_kv_key,
        _mock_assign_acr_role,
        _mock_assign_kv_role,
        _mock_create_identity,
        _mock_config_json,
        _mock_generate_config,
    ):
        provision_tenant(str(self.tenant.id))
        _mock_assign_kv_role.assert_not_called()

    @override_settings(OPENCLAW_CONTAINER_SECRET_BACKEND="keyvault")
    @patch("apps.orchestrator.services.create_container_app", side_effect=RuntimeError("azure error"))
    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch("apps.orchestrator.services.register_environment_storage")
    @patch("apps.orchestrator.services.create_tenant_file_share")
    @patch(
        "apps.orchestrator.services.store_tenant_internal_key_in_key_vault",
        return_value="tenant-xxx-internal-key",
    )
    @patch("apps.orchestrator.services.assign_acr_pull_role")
    @patch("apps.orchestrator.services.assign_key_vault_role")
    @patch(
        "apps.orchestrator.services.create_managed_identity",
        return_value={"id": "/identities/2", "client_id": "client-2", "principal_id": "principal-2"},
    )
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    def test_provision_failure_resets_to_pending(
        self,
        _mock_generate_config,
        _mock_config_json,
        _mock_create_identity,
        _mock_assign_kv_role,
        _mock_assign_acr_role,
        _mock_store_kv_key,
        _mock_create_file_share,
        _mock_register_storage,
        _mock_upload_config,
        _mock_create_container,
    ):
        with self.assertRaises(RuntimeError):
            provision_tenant(str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.PENDING)
        _mock_assign_kv_role.assert_called_once_with("principal-2")

    @patch("apps.orchestrator.services.create_container_app")
    def test_provision_skips_if_tenant_not_provisionable(self, mock_create_container):
        self.tenant.status = Tenant.Status.DELETED
        self.tenant.save(update_fields=["status", "updated_at"])

        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        self.assertEqual(self.tenant.status, Tenant.Status.DELETED)
        mock_create_container.assert_not_called()

    @patch("apps.orchestrator.services.delete_managed_identity")
    @patch("apps.orchestrator.services.delete_tenant_file_share")
    @patch("apps.orchestrator.services.delete_container_app")
    def test_deprovision_clears_container_fields(self, mock_delete_container, mock_delete_file_share, mock_delete_identity):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-tenant"
        self.tenant.container_fqdn = "oc-tenant.internal.azurecontainerapps.io"
        self.tenant.managed_identity_id = "/identities/3"
        self.tenant.save(
            update_fields=["status", "container_id", "container_fqdn", "managed_identity_id", "updated_at"]
        )

        deprovision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        self.assertEqual(self.tenant.status, Tenant.Status.DELETED)
        self.assertEqual(self.tenant.container_id, "")
        self.assertEqual(self.tenant.container_fqdn, "")
        self.assertEqual(self.tenant.managed_identity_id, "")
        mock_delete_container.assert_called_once_with("oc-tenant")
        mock_delete_identity.assert_called_once_with(str(self.tenant.id))

    @patch("apps.orchestrator.services.delete_tenant_file_share")
    @patch("apps.orchestrator.services.delete_container_app", side_effect=RuntimeError("delete failed"))
    def test_deprovision_failure_marks_suspended(self, _mock_delete_container, _mock_delete_file_share):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-failing"
        self.tenant.save(update_fields=["status", "container_id", "updated_at"])

        with self.assertRaises(RuntimeError):
            deprovision_tenant(str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.SUSPENDED)


class SeedCronJobsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Cron Seeder", telegram_chat_id=515152)

    @patch("time.sleep")
    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_creates_jobs_via_gateway(
        self,
        mock_invoke,
        mock_sleep,
    ):
        mock_invoke.side_effect = [
            {"jobs": []},
            {"name": "Morning Briefing", "enabled": True},
            {"name": "Evening Check-in", "enabled": True},
            {"name": "Week Ahead Review", "enabled": True},
            {"name": "Background Tasks", "enabled": True},
        ]

        result = seed_cron_jobs(self.tenant)

        self.assertEqual(result["created"], 4)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(mock_invoke.call_count, 5)
        self.assertEqual(mock_invoke.call_args_list[0].args[1], "cron.list")
        self.assertEqual(mock_invoke.call_args_list[1].args[1], "cron.add")
        self.assertEqual(mock_invoke.call_args_list[2].args[1], "cron.add")
        self.assertEqual(mock_invoke.call_args_list[3].args[1], "cron.add")
        self.assertEqual(mock_invoke.call_args_list[4].args[1], "cron.add")
        mock_sleep.assert_not_called()

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_skips_when_jobs_exist(
        self,
        mock_invoke,
    ):
        mock_invoke.return_value = {"jobs": [{"name": "existing"}]}

        result = seed_cron_jobs(self.tenant)

        self.assertTrue(result.get("skipped"))
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(mock_invoke.call_count, 1)
        self.assertEqual(mock_invoke.call_args.args[0], self.tenant)
        self.assertEqual(mock_invoke.call_args.args[1], "cron.list")


    @patch("time.sleep")
    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_handles_add_failure(
        self,
        mock_invoke,
        mock_sleep,
    ):
        mock_invoke.side_effect = [
            {"jobs": []},
            {"name": "Morning Briefing", "enabled": True},
            GatewayError("temporary API error"),
            {"name": "Week Ahead Review", "enabled": True},
            {"name": "Background Tasks", "enabled": True},
        ]

        result = seed_cron_jobs(self.tenant)

        self.assertEqual(result["created"], 3)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(mock_invoke.call_count, 5)
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_retries_on_transient_error(
        self,
        mock_invoke,
        mock_sleep,
    ):
        mock_invoke.side_effect = [
            {"jobs": []},
            GatewayError("temporary", status_code=502),
            {"name": "Morning Briefing", "enabled": True},
            {"name": "Evening Check-in", "enabled": True},
            {"name": "Week Ahead Review", "enabled": True},
            {"name": "Background Tasks", "enabled": True},
        ]

        result = seed_cron_jobs(self.tenant)

        self.assertEqual(result["created"], 4)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(mock_invoke.call_count, 6)
        mock_sleep.assert_called_once_with(5)

    @patch("time.sleep")
    @patch("apps.orchestrator.services._is_mock", return_value=True)
    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_mock_mode(
        self,
        mock_invoke,
        mock_is_mock,
        mock_sleep,
    ):
        result = seed_cron_jobs(self.tenant)

        self.assertEqual(result["created"], 4)
        self.assertEqual(result["errors"], 0)
        self.assertFalse(result.get("skipped", False))
        mock_invoke.assert_not_called()


class RepairTenantProvisioningCommandTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Repair", telegram_chat_id=515190)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = ""
        self.tenant.container_fqdn = ""
        self.tenant.save(update_fields=["status", "container_id", "container_fqdn", "updated_at"])

    @patch("apps.orchestrator.services.provision_tenant")
    def test_dry_run_selects_target_without_provision_call(self, mock_provision):
        out = StringIO()
        call_command("repair_tenant_provisioning", "--dry-run", stdout=out)

        output = out.getvalue()
        self.assertIn("evaluated=1", output)
        self.assertIn("dry_run=True", output)
        self.assertIn(f"tenant_id={self.tenant.id}", output)
        mock_provision.assert_not_called()

    @patch("apps.orchestrator.services.provision_tenant")
    def test_limit_applies_and_invokes_provision(self, mock_provision):
        out = StringIO()
        call_command("repair_tenant_provisioning", "--limit", "1", stdout=out)
        mock_provision.assert_called_once_with(str(self.tenant.id))


class CronTaskMapWiringTest(TestCase):
    def test_repair_task_is_wired(self):
        from apps.cron.views import TASK_MAP

        self.assertEqual(
            TASK_MAP["repair_stale_tenant_provisioning"],
            "apps.orchestrator.tasks.repair_stale_tenant_provisioning_task",
        )
