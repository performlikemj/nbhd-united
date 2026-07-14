"""Tests for DEK-minting wiring in provision_tenant / deprovision_tenant
(Encryption-at-rest Phase 1 PR5).

Covers the T3 dark-rollout guarantee: when minting succeeds, keys must
exist before the container is created and must never be threaded into the
container spec/env — and a KEK soft-delete failure during deprovision must
never block the rest of deprovision.

Also covers the fail-closed mint posture (restored 2026-07-11, PR B):
now that `kv-nbhd-keks` + the provisioner/broker RBAC are live and every
pre-existing tenant has a DEK (via the 2026-07 backfill), a DEK-mint failure
is a real error — provisioning must abort and reset the tenant to PENDING
rather than strand a container-having, DEK-less (un-encryptable) tenant.
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


# Force the azure_client mock branch explicitly rather than relying on the
# ambient AZURE_MOCK env var. In CI's full-suite run os.environ is polluted
# by earlier tests, so `_is_mock()` can flip False mid-run and
# provision_tenant -> mint_and_wrap_dek -> create_tenant_kek would reach for
# REAL Key Vault. Patching `_is_mock` on the azure_client module (where the
# DEK-mint path calls it) is strictly more robust than the env var — matches
# the merged PR1 test (test_azure_client_keys.py).
@override_settings(OPENCLAW_CONTAINER_SECRET_BACKEND="keyvault")
@patch("apps.orchestrator.azure_client._is_mock", return_value=True)
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
        _mock_is_mock,
    ):
        # mint_and_wrap_dek runs for real against the stateful mock KEK
        # registry (create_container_app etc. are mocked, but the DEK-mint
        # path is NOT — this proves the actual TenantDek row + KEK get
        # created, not just that a function was called).
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)

        # Encryption-at-rest Phase 2: a freshly-provisioned tenant is chat-
        # encrypted from its very first message — the write-flag flips ON at
        # provision (right after the fail-closed DEK mint proves a key exists)
        # and the read-flag follows because the verify gate finds ZERO
        # plaintext-only rows (this tenant has no pre-provision chat). Without
        # this the tenant would silently write+read plaintext forever (the
        # production gap this PR closes). The pre-provision-rows case is
        # test_pre_provision_plaintext_rows_defer_read_flip_to_converge.
        self.assertTrue(self.tenant.encrypt_chat_writes)
        self.assertTrue(self.tenant.read_encrypted_chat)

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
        return_value={"name": "oc-prov5-tenant-2", "fqdn": "oc-prov5-tenant-2.internal.azurecontainerapps.io"},
    )
    @patch("apps.orchestrator.services._audit_and_log")
    @patch("apps.crypto.keys.mint_and_wrap_dek", side_effect=RuntimeError("KEK vault unreachable"))
    def test_dek_mint_failure_aborts_provisioning_fail_closed(
        self,
        _mock_mint,
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
        _mock_is_mock,
    ):
        # Fail-closed posture (restored 2026-07-11): kv-nbhd-keks + broker RBAC
        # are live and every tenant has a DEK via the 2026-07 backfill, so a
        # mint failure is now a real error. Provisioning must propagate it and
        # reset the tenant to PENDING (like any other critical-step failure —
        # see test_provision_failure_resets_to_pending), NOT swallow it and
        # strand a container-having, DEK-less tenant.
        with self.assertRaises(RuntimeError):
            provision_tenant(str(self.tenant.id))

        self.tenant.refresh_from_db()
        # Reset to PENDING by the provision_tenant error handler, so the
        # natural QStash retry / re-provision path picks it back up.
        self.assertEqual(self.tenant.status, Tenant.Status.PENDING)

        # The mint aborts BEFORE the container is created (mint precedes
        # create_container_app in provision_tenant), so no half-provisioned
        # container is left behind.
        mock_create_container.assert_not_called()

        # No DEK row was written.
        self.assertFalse(TenantDek.objects.filter(tenant=self.tenant).exists())

    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch(
        "apps.orchestrator.services.create_managed_identity",
        return_value={"id": "/identities/prov5-3", "client_id": "client-prov5-3", "principal_id": "principal-prov5-3"},
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
        return_value={"name": "oc-prov5-tenant-3", "fqdn": "oc-prov5-tenant-3.internal.azurecontainerapps.io"},
    )
    @patch("apps.orchestrator.services._audit_and_log")
    def test_pre_provision_plaintext_rows_defer_read_flip_to_converge(
        self,
        _mock_audit,
        _mock_create_container,
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
        _mock_is_mock,
    ):
        # The early-write window is REAL: the chat endpoints deliberately have
        # no tenant.status gate (messaging while the container spins up is a
        # supported flow), so a plaintext-only AppChatMessage/ChatThread row can
        # exist BEFORE provision_tenant runs. Local imports: only this test
        # touches the chat/crypto surfaces.
        from io import StringIO

        from django.core.management import call_command

        from apps.crypto import box
        from apps.orchestrator.management.commands.converge_unencrypted_chat_tenants import (
            Command as ConvergeCommand,
        )
        from apps.router import enc_columns
        from apps.router.models import AppChatMessage, ChatThread

        thread = ChatThread.objects.create(tenant=self.tenant, user=self.tenant.user, title="Early", is_main=False)
        msg = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.tenant.user,
            thread=thread,
            client_msg_id="early-1",
            user_text="sent while provisioning",
            status=AppChatMessage.Status.PENDING,
        )

        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)

        # Verify-gated flip: writes ON (every row from here dual-writes _enc),
        # reads OFF — an unconditional both-flags flip would make this tenant
        # read as fully-converged, the converge command would skip it forever,
        # and the irreversible PR-6 erase would destroy msg's plaintext as the
        # only copy.
        self.assertTrue(self.tenant.encrypt_chat_writes)
        self.assertFalse(self.tenant.read_encrypted_chat)

        # (writes=ON, reads=OFF) is exactly the shape the converge command
        # picks up — the safety net for this tenant.
        candidates = ConvergeCommand()._candidates(None)
        self.assertIn(self.tenant.id, [t.id for t in candidates])

        # Running the converge task seals the early rows and flips reads.
        out = StringIO()
        call_command("converge_unencrypted_chat_tenants", "--tenant-id", str(self.tenant.id), stdout=out)
        self.assertIn("Converged 1", out.getvalue())

        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.read_encrypted_chat)
        msg.refresh_from_db()
        thread.refresh_from_db()
        self.assertEqual(
            box.decrypt(self.tenant.id, *enc_columns.APP_CHAT_MESSAGE_USER_TEXT, bytes(msg.user_text_enc)).reveal(),
            "sent while provisioning",
        )
        self.assertEqual(
            box.decrypt(self.tenant.id, *enc_columns.CHAT_THREAD_TITLE, bytes(thread.title_enc)).reveal(),
            "Early",
        )


@patch("apps.orchestrator.azure_client._is_mock", return_value=True)
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
        self,
        _mock_delete_container,
        _mock_delete_file_share,
        _mock_delete_identity,
        mock_begin_delete_kek,
        _mock_is_mock,
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
        self,
        _mock_delete_container,
        _mock_delete_file_share,
        _mock_delete_identity,
        mock_begin_delete_kek,
        _mock_is_mock,
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
