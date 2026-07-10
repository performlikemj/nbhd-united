"""Tests for DEK-minting wiring in provision_tenant / deprovision_tenant
(Encryption-at-rest Phase 1 PR5).

Covers the T3 dark-rollout guarantee: keys must exist before the container
is created, but must never be threaded into the container spec/env — and a
KEK soft-delete failure during deprovision must never block the rest of
deprovision.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.orchestrator.services import deprovision_tenant, provision_tenant
from apps.tenants.models import Tenant, TenantDek
from apps.tenants.services import create_tenant

# The exact keyword-argument surface `create_container_app` is called with
# in provision_tenant today. Any key outside this set — in particular
# anything DEK/KEK-shaped — would mean key material started leaking into
# the container spec, which is the one thing Phase 1 must never do.
_EXPECTED_CONTAINER_SPEC_KEYS = {
    "tenant_id",
    "container_name",
    "config_json",
    "identity_id",
    "identity_client_id",
    "workspace_env",
    "internal_api_key_kv_secret_name",
    "internal_api_key_plain_value",
    "openrouter_kv_secret_name",
}


@override_settings(OPENCLAW_CONTAINER_SECRET_BACKEND="keyvault")
class ProvisionMintsDekTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Crypto Provision", telegram_chat_id=616161)

    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch(
        "apps.orchestrator.services.create_managed_identity",
        return_value={"id": "/identities/prov5-1", "client_id": "client-prov5-1", "principal_id": "principal-prov5-1"},
    )
    @patch(
        "apps.orchestrator.services.store_tenant_internal_key_in_key_vault",
        return_value="tenant-fixture-internal-key",
    )
    @patch("apps.orchestrator.services.assign_key_vault_role")
    @patch("apps.orchestrator.services.assign_acr_pull_role")
    @patch(
        "apps.orchestrator.services.seed_cron_jobs",
        return_value={"tenant_id": "seed", "jobs_total": 5, "created": 5, "errors": 0},
    )
    @patch("apps.cron.views._schedule_qstash_task", create=True, return_value=None)
    @patch("apps.orchestrator.services.create_tenant_file_share")
    @patch("apps.orchestrator.services.register_environment_storage")
    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch(
        "apps.orchestrator.services.create_container_app",
        return_value={"name": "oc-prov5-tenant", "fqdn": "oc-prov5-tenant.internal.azurecontainerapps.io"},
    )
    @patch("apps.orchestrator.services._audit_and_log")
    def test_fresh_provision_mints_kek_and_epoch0_dek_with_no_container_key_ref(
        self,
        _mock_audit,
        mock_create_container,
        _mock_upload_config,
        _mock_register_storage,
        _mock_create_file_share,
        _mock_schedule_qstash,
        _mock_seed_cron_jobs,
        _mock_assign_acr_role,
        _mock_assign_kv_role,
        _mock_store_kv_key,
        _mock_create_identity,
        _mock_config_json,
        _mock_generate_config,
    ):
        # mint_and_wrap_dek runs for real against the stateful AZURE_MOCK
        # KEK registry (not mocked out) — this proves the actual TenantDek
        # row + KEK get created, not just that a function was called.
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)

        dek_row = TenantDek.objects.get(tenant=self.tenant, dek_epoch=0)
        self.assertEqual(dek_row.kek_version, "mock-v1")
        self.assertTrue(bytes(dek_row.wrapped_dek))

        # The container was actually created (mint happened before it, not
        # instead of it) ...
        mock_create_container.assert_called_once()
        call = mock_create_container.call_args

        # ... but the spec passed to it carries NO DEK/KEK reference: exact
        # keyword surface match, plus a belt-and-suspenders substring scan
        # of every stringified value.
        self.assertEqual(call.args, ())
        self.assertEqual(set(call.kwargs.keys()), _EXPECTED_CONTAINER_SPEC_KEYS)
        spec_text = " ".join(str(v).lower() for v in call.kwargs.values())
        self.assertNotIn("dek", spec_text)
        self.assertNotIn("kek", spec_text)
        self.assertNotIn(bytes(dek_row.wrapped_dek).hex(), spec_text)

    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch(
        "apps.orchestrator.services.create_managed_identity",
        return_value={"id": "/identities/prov5-2", "client_id": "client-prov5-2", "principal_id": "principal-prov5-2"},
    )
    @patch(
        "apps.orchestrator.services.store_tenant_internal_key_in_key_vault",
        return_value="tenant-fixture-internal-key",
    )
    @patch("apps.orchestrator.services.create_container_app")
    @patch("apps.orchestrator.services._audit_and_log")
    @patch("apps.crypto.keys.mint_and_wrap_dek", side_effect=RuntimeError("KEK vault unreachable"))
    def test_dek_mint_failure_aborts_before_container_create(
        self,
        _mock_mint,
        _mock_audit,
        mock_create_container,
        _mock_store_kv_key,
        _mock_create_identity,
        _mock_config_json,
        _mock_generate_config,
    ):
        with self.assertRaises(RuntimeError):
            provision_tenant(str(self.tenant.id))

        self.tenant.refresh_from_db()
        # Provisioning failed cleanly: tenant reset to PENDING, exactly as
        # the pre-existing failure path (test_provision_failure_resets_to_pending)
        # already proves for other early-stage failures.
        self.assertEqual(self.tenant.status, Tenant.Status.PENDING)
        self.assertEqual(self.tenant.container_id, "")

        # No orphan container: create_container_app must never be reached.
        mock_create_container.assert_not_called()

        # No orphan DEK row either — the mint call itself raised.
        self.assertFalse(TenantDek.objects.filter(tenant=self.tenant).exists())


class DeprovisionKekSoftDeleteTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Crypto Deprovision", telegram_chat_id=616162)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-dek-deprov"
        self.tenant.container_fqdn = "oc-dek-deprov.internal.azurecontainerapps.io"
        self.tenant.managed_identity_id = "/identities/dek-deprov"
        self.tenant.save(
            update_fields=["status", "container_id", "container_fqdn", "managed_identity_id", "updated_at"]
        )

    @patch("apps.orchestrator.services.begin_delete_kek")
    @patch("apps.orchestrator.services.delete_managed_identity")
    @patch("apps.orchestrator.services.delete_tenant_file_share")
    @patch("apps.orchestrator.services.delete_container_app")
    def test_deprovision_calls_begin_delete_kek(
        self, _mock_delete_container, _mock_delete_file_share, _mock_delete_identity, mock_begin_delete_kek
    ):
        deprovision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        self.assertEqual(self.tenant.status, Tenant.Status.DELETED)
        mock_begin_delete_kek.assert_called_once_with(str(self.tenant.id))

    @patch("apps.orchestrator.services.begin_delete_kek", side_effect=RuntimeError("Key Vault throttled"))
    @patch("apps.orchestrator.services.delete_managed_identity")
    @patch("apps.orchestrator.services.delete_tenant_file_share")
    @patch("apps.orchestrator.services.delete_container_app")
    def test_deprovision_tolerates_kek_delete_failure(
        self, _mock_delete_container, _mock_delete_file_share, _mock_delete_identity, mock_begin_delete_kek
    ):
        # Must NOT raise — a KEK soft-delete failure has its own
        # log-and-continue try/except and must never block the rest of
        # deprovision (compare test_deprovision_failure_marks_suspended,
        # which DOES raise for a failure on the main try/except).
        deprovision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        mock_begin_delete_kek.assert_called_once_with(str(self.tenant.id))
        self.assertEqual(self.tenant.status, Tenant.Status.DELETED)
        self.assertEqual(self.tenant.container_id, "")
        self.assertEqual(self.tenant.managed_identity_id, "")
