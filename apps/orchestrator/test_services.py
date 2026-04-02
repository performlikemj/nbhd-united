"""Additional orchestrator service coverage."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.cron.gateway_client import GatewayError
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.orchestrator.services import dedup_tenant_cron_jobs, deprovision_tenant, provision_tenant, seed_cron_jobs


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
    @patch("apps.orchestrator.services.seed_cron_jobs", return_value={"tenant_id": "seed", "jobs_total": 5, "created": 5, "errors": 0})
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
        _mock_create_container.assert_called_once()
        # All containers use the shared internal API key from Key Vault
        call_kwargs = _mock_create_container.call_args.kwargs
        self.assertNotIn("internal_api_key_kv_secret", call_kwargs)

    @override_settings(OPENCLAW_CONTAINER_SECRET_BACKEND="env")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch(
        "apps.orchestrator.services.create_managed_identity",
        return_value={"id": "/identities/10", "client_id": "client-10", "principal_id": "principal-10"},
    )
    @patch("apps.orchestrator.services.assign_key_vault_role")
    @patch("apps.orchestrator.services.assign_acr_pull_role")
    @patch("apps.orchestrator.services.seed_cron_jobs", return_value={"tenant_id": "seed", "jobs_total": 5, "created": 5, "errors": 0})
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

    @override_settings(OPENCLAW_CONTAINER_SECRET_BACKEND="keyvault")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch(
        "apps.orchestrator.services.create_managed_identity",
        return_value={"id": "/identities/3", "client_id": "client-3", "principal_id": "principal-3"},
    )
    @patch("apps.orchestrator.services.assign_key_vault_role")
    @patch("apps.orchestrator.services.assign_acr_pull_role")
    @patch("apps.orchestrator.services.seed_cron_jobs", side_effect=RuntimeError("gateway down"))
    @patch("apps.cron.views._schedule_qstash_task", create=True, side_effect=RuntimeError("qstash unavailable"))
    @patch("apps.orchestrator.services.create_tenant_file_share")
    @patch("apps.orchestrator.services.register_environment_storage")
    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch(
        "apps.orchestrator.services.create_container_app",
        return_value={"name": "oc-tenant", "fqdn": "oc-tenant.internal.azurecontainerapps.io"},
    )
    def test_post_provision_failure_preserves_active_status(
        self,
        _mock_create_container,
        _mock_upload_config,
        _mock_register_storage,
        _mock_create_file_share,
        _mock_schedule_qstash,
        _mock_seed_cron_jobs,
        _mock_assign_acr_role,
        _mock_assign_kv_role,
        _mock_create_identity,
        _mock_config_json,
        _mock_generate_config,
    ):
        """Post-provision failures (welcome msg, cron seeding) must not reset to PENDING."""
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        self.assertEqual(self.tenant.container_id, "oc-tenant")
        self.assertEqual(self.tenant.container_fqdn, "oc-tenant.internal.azurecontainerapps.io")
        self.assertIsNotNone(self.tenant.provisioned_at)

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
    @patch("apps.orchestrator.services._is_mock", return_value=False)
    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_creates_jobs_via_gateway(
        self,
        mock_invoke,
        _mock_is_mock,
        mock_sleep,
    ):
        mock_invoke.side_effect = [
            {"jobs": []},  # initial cron.list
            {"name": "Morning Briefing", "enabled": True},
            {"name": "Evening Check-in", "enabled": True},
            {"name": "Weekly Reflection", "enabled": True},
            {"name": "Week Ahead Review", "enabled": True},
            {"name": "Background Tasks", "enabled": True},
            {"name": "Heartbeat Check-in", "enabled": True},
            {"jobs": []},  # dedup pass cron.list (no dupes)
        ]

        result = seed_cron_jobs(self.tenant)

        self.assertEqual(result["created"], 6)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(mock_invoke.call_args_list[0].args[1], "cron.list")
        for i in range(1, 7):
            self.assertEqual(mock_invoke.call_args_list[i].args[1], "cron.add")
        mock_sleep.assert_not_called()

    @patch("apps.orchestrator.services._is_mock", return_value=False)
    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_skips_when_all_jobs_exist(
        self,
        mock_invoke,
        _mock_is_mock,
    ):
        mock_invoke.return_value = {"jobs": [
            {"name": "Morning Briefing"},
            {"name": "Evening Check-in"},
            {"name": "Weekly Reflection"},
            {"name": "Week Ahead Review"},
            {"name": "Background Tasks"},
            {"name": "Heartbeat Check-in"},
        ]}

        result = seed_cron_jobs(self.tenant)

        self.assertTrue(result.get("skipped"))
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["errors"], 0)
        # Only the initial cron.list call — no adds, no dedup
        self.assertEqual(mock_invoke.call_count, 1)
        self.assertEqual(mock_invoke.call_args.args[1], "cron.list")

    @patch("time.sleep")
    @patch("apps.orchestrator.services._is_mock", return_value=False)
    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_creates_only_missing_jobs(
        self,
        mock_invoke,
        _mock_is_mock,
        mock_sleep,
    ):
        """When some jobs already exist, only the missing ones are created."""
        mock_invoke.side_effect = [
            # initial cron.list — 3 of 6 already exist
            {"jobs": [
                {"name": "Morning Briefing"},
                {"name": "Evening Check-in"},
                {"name": "Week Ahead Review"},
            ]},
            # cron.add for the 3 missing jobs
            {"name": "Weekly Reflection", "enabled": True},
            {"name": "Background Tasks", "enabled": True},
            {"name": "Heartbeat Check-in", "enabled": True},
            # dedup pass cron.list
            {"jobs": []},
        ]

        result = seed_cron_jobs(self.tenant)

        self.assertEqual(result["created"], 3)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["skipped_existing"], 3)
        # 1 list + 3 adds + 1 dedup list = 5
        self.assertEqual(mock_invoke.call_count, 5)
        # Verify the add calls are for the right tool
        for i in range(1, 4):
            self.assertEqual(mock_invoke.call_args_list[i].args[1], "cron.add")
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("apps.orchestrator.services._is_mock", return_value=False)
    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_handles_add_failure(
        self,
        mock_invoke,
        _mock_is_mock,
        mock_sleep,
    ):
        mock_invoke.side_effect = [
            {"jobs": []},
            {"name": "Morning Briefing", "enabled": True},
            GatewayError("temporary API error"),
            {"name": "Weekly Reflection", "enabled": True},
            {"name": "Week Ahead Review", "enabled": True},
            {"name": "Background Tasks", "enabled": True},
            {"name": "Heartbeat Check-in", "enabled": True},
            {"jobs": []},  # dedup pass
        ]

        result = seed_cron_jobs(self.tenant)

        self.assertEqual(result["created"], 5)
        self.assertEqual(result["errors"], 1)
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("apps.orchestrator.services._is_mock", return_value=False)
    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_seed_retries_on_transient_error(
        self,
        mock_invoke,
        _mock_is_mock,
        mock_sleep,
    ):
        mock_invoke.side_effect = [
            {"jobs": []},
            GatewayError("temporary", status_code=502),
            {"name": "Morning Briefing", "enabled": True},
            {"name": "Evening Check-in", "enabled": True},
            {"name": "Weekly Reflection", "enabled": True},
            {"name": "Week Ahead Review", "enabled": True},
            {"name": "Background Tasks", "enabled": True},
            {"name": "Heartbeat Check-in", "enabled": True},
            {"jobs": []},  # dedup pass
        ]

        result = seed_cron_jobs(self.tenant)

        self.assertEqual(result["created"], 6)
        self.assertEqual(result["errors"], 0)
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

        self.assertEqual(result["created"], 5)
        self.assertEqual(result["errors"], 0)
        self.assertFalse(result.get("skipped", False))
        mock_invoke.assert_not_called()

    def test_cron_seed_jobs_all_use_agent_turn_payload(self):
        """All cron seed jobs must use kind=agentTurn — gateway rejects other kinds."""
        from apps.orchestrator.config_generator import build_cron_seed_jobs

        jobs = build_cron_seed_jobs(self.tenant)
        for job in jobs:
            self.assertEqual(
                job["payload"]["kind"],
                "agentTurn",
                f"Job '{job['name']}' uses payload kind='{job['payload']['kind']}' "
                f"— OpenClaw gateway only supports 'agentTurn'",
            )


class DedupTenantCronJobsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Dedup Test", telegram_chat_id=515153)

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_dedup_keeps_newest_by_created_at(self, mock_invoke):
        """When duplicates exist, the newest job (by createdAt) is kept."""
        jobs = [
            {"name": "Morning Briefing", "id": "old-1", "createdAt": "2026-01-01T00:00:00Z"},
            {"name": "Morning Briefing", "id": "new-1", "createdAt": "2026-03-01T00:00:00Z"},
            {"name": "Morning Briefing", "id": "mid-1", "createdAt": "2026-02-01T00:00:00Z"},
            {"name": "Evening Check-in", "id": "only-1", "createdAt": "2026-01-01T00:00:00Z"},
        ]
        mock_invoke.side_effect = [
            {"jobs": jobs},  # cron.list
            {},  # cron.remove old-1
            {},  # cron.remove mid-1
        ]

        result = dedup_tenant_cron_jobs(self.tenant)

        self.assertEqual(result["kept"], 2)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["errors"], 0)
        # Verify the right jobs were deleted (old and mid, not new)
        remove_calls = [
            c for c in mock_invoke.call_args_list
            if c.args[1] == "cron.remove"
        ]
        deleted_ids = {c.kwargs.get("jobId") or c.args[2].get("jobId") for c in remove_calls}
        self.assertIn("mid-1", deleted_ids)
        self.assertIn("old-1", deleted_ids)
        self.assertNotIn("new-1", deleted_ids)

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_dedup_no_duplicates(self, mock_invoke):
        """When no duplicates exist, nothing is deleted."""
        mock_invoke.return_value = {"jobs": [
            {"name": "Morning Briefing", "id": "1"},
            {"name": "Evening Check-in", "id": "2"},
        ]}

        result = dedup_tenant_cron_jobs(self.tenant)

        self.assertEqual(result["kept"], 2)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(len(result["duplicates"]), 0)
        self.assertEqual(mock_invoke.call_count, 1)  # only cron.list

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_dedup_dry_run(self, mock_invoke):
        """Dry run reports duplicates without deleting."""
        jobs = [
            {"name": "Morning Briefing", "id": "old-1", "createdAt": "2026-01-01T00:00:00Z"},
            {"name": "Morning Briefing", "id": "new-1", "createdAt": "2026-03-01T00:00:00Z"},
        ]
        mock_invoke.return_value = {"jobs": jobs}

        result = dedup_tenant_cron_jobs(self.tenant, dry_run=True)

        self.assertEqual(result["deleted"], 0)
        self.assertEqual(len(result["duplicates"]), 1)
        self.assertEqual(result["duplicates"][0]["id"], "old-1")
        self.assertEqual(mock_invoke.call_count, 1)  # only cron.list, no removes

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_dedup_with_prefetched_jobs(self, mock_invoke):
        """When jobs are pre-fetched, no cron.list call is made."""
        jobs = [
            {"name": "Morning Briefing", "id": "old-1", "createdAt": "2026-01-01T00:00:00Z"},
            {"name": "Morning Briefing", "id": "new-1", "createdAt": "2026-03-01T00:00:00Z"},
        ]
        mock_invoke.return_value = {}  # for cron.remove

        result = dedup_tenant_cron_jobs(self.tenant, jobs=jobs)

        self.assertEqual(result["deleted"], 1)
        # Only cron.remove call, no cron.list
        self.assertEqual(mock_invoke.call_count, 1)
        self.assertEqual(mock_invoke.call_args.args[1], "cron.remove")


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
