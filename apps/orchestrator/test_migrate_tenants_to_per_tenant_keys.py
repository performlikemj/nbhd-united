"""Tests for `migrate_tenants_to_per_tenant_keys` management command (Phase 1c)."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.orchestrator.management.commands.migrate_tenants_to_per_tenant_keys import Command
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class MigrateTenantsToPerTenantKeysTest(TestCase):
    def setUp(self):
        # Migration candidate: ACTIVE, has container + MI, no per-tenant key yet.
        self.tenant_unmigrated = create_tenant(display_name="Unmigrated", telegram_chat_id=900101)
        self.tenant_unmigrated.status = Tenant.Status.ACTIVE
        self.tenant_unmigrated.container_id = "oc-unmigrated"
        self.tenant_unmigrated.managed_identity_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ManagedIdentity/"
            "userAssignedIdentities/mi-nbhd-unmigrated"
        )
        self.tenant_unmigrated.save()

        # Already migrated — must be skipped.
        self.tenant_migrated = create_tenant(display_name="Migrated", telegram_chat_id=900102)
        self.tenant_migrated.status = Tenant.Status.ACTIVE
        self.tenant_migrated.container_id = "oc-migrated"
        self.tenant_migrated.managed_identity_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ManagedIdentity/"
            "userAssignedIdentities/mi-nbhd-migrated"
        )
        self.tenant_migrated.internal_api_key = "preexisting-per-tenant-key"
        self.tenant_migrated.save()

        # Pending — no container/MI yet, must be skipped.
        self.tenant_pending = create_tenant(display_name="Pending", telegram_chat_id=900103)

    def test_candidate_filter_skips_already_migrated_and_pending(self):
        candidates = Command()._candidates(None)
        ids = {str(c.id) for c in candidates}
        self.assertIn(str(self.tenant_unmigrated.id), ids)
        self.assertNotIn(str(self.tenant_migrated.id), ids)
        self.assertNotIn(str(self.tenant_pending.id), ids)

    def test_tenant_id_filter_targets_one_tenant(self):
        candidates = Command()._candidates(str(self.tenant_unmigrated.id))
        ids = {str(c.id) for c in candidates}
        self.assertEqual(ids, {str(self.tenant_unmigrated.id)})

    def test_dry_run_makes_no_changes(self):
        out = StringIO()
        call_command("migrate_tenants_to_per_tenant_keys", "--dry-run", stdout=out)
        self.tenant_unmigrated.refresh_from_db()
        self.assertEqual(self.tenant_unmigrated.internal_api_key, "")
        self.assertIn("dry-run", out.getvalue())

    @patch(
        "apps.orchestrator.management.commands.migrate_tenants_to_per_tenant_keys.update_container_internal_api_key_secret"
    )
    @patch("apps.orchestrator.management.commands.migrate_tenants_to_per_tenant_keys.assign_key_vault_role")
    @patch(
        "apps.orchestrator.management.commands.migrate_tenants_to_per_tenant_keys.store_tenant_internal_key_in_key_vault",
        return_value="tenant-fixture-internal-key",
    )
    @patch.object(Command, "_lookup_principal_id", return_value="principal-fixture")
    def test_happy_path_full_sequence(
        self,
        mock_lookup,
        mock_store_kv,
        mock_grant_role,
        mock_update_ca,
    ):
        call_command(
            "migrate_tenants_to_per_tenant_keys",
            "--tenant-id",
            str(self.tenant_unmigrated.id),
        )

        # KV write happened with tenant id + a freshly-generated token.
        mock_store_kv.assert_called_once()
        kv_args = mock_store_kv.call_args.args
        self.assertEqual(kv_args[0], str(self.tenant_unmigrated.id))
        self.assertGreater(len(kv_args[1]), 32, "generated token should be long")

        # Role grant scoped to the new per-tenant KV secret.
        mock_grant_role.assert_called_once()
        self.assertEqual(mock_grant_role.call_args.args[0], "principal-fixture")
        self.assertEqual(
            mock_grant_role.call_args.kwargs["secret_names"],
            ["tenant-fixture-internal-key"],
        )

        # CA spec rebind hit the right container with the right identity.
        mock_update_ca.assert_called_once_with(
            container_name="oc-unmigrated",
            identity_id=self.tenant_unmigrated.managed_identity_id,
            kv_secret_name="tenant-fixture-internal-key",
        )

        # DB save happened last and contains the same token that was
        # written to KV.
        self.tenant_unmigrated.refresh_from_db()
        self.assertNotEqual(self.tenant_unmigrated.internal_api_key, "")
        self.assertEqual(self.tenant_unmigrated.internal_api_key, kv_args[1])

    @patch(
        "apps.orchestrator.management.commands.migrate_tenants_to_per_tenant_keys.update_container_internal_api_key_secret",
        side_effect=RuntimeError("CA update failed"),
    )
    @patch("apps.orchestrator.management.commands.migrate_tenants_to_per_tenant_keys.assign_key_vault_role")
    @patch(
        "apps.orchestrator.management.commands.migrate_tenants_to_per_tenant_keys.store_tenant_internal_key_in_key_vault",
        return_value="tenant-fixture-internal-key",
    )
    @patch.object(Command, "_lookup_principal_id", return_value="principal-fixture")
    def test_ca_update_failure_leaves_db_unmigrated(
        self,
        mock_lookup,
        mock_store_kv,
        mock_grant_role,
        mock_update_ca,
    ):
        """If the CA spec update fails, the DB save must NOT happen — so
        a retry regenerates and overwrites cleanly. Without this guarantee
        we could end up with DB pointing at a key the container never
        learned about, breaking auth."""
        out = StringIO()
        err = StringIO()
        call_command(
            "migrate_tenants_to_per_tenant_keys",
            "--tenant-id",
            str(self.tenant_unmigrated.id),
            stdout=out,
            stderr=err,
        )
        self.tenant_unmigrated.refresh_from_db()
        self.assertEqual(self.tenant_unmigrated.internal_api_key, "")
        self.assertIn("FAIL", err.getvalue())
