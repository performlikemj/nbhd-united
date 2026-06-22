"""Adversarial audit A38 — deprovision_tenant partial-failure field clearing.

Verifies that openrouter_key_hash and openrouter_key_secret_name are cleared
in the except handler when container/file-share/identity teardown raises,
preventing a reactivated SUSPENDED tenant from referencing deleted OR resources.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .services import deprovision_tenant, provision_tenant


class DeprovisionPartialFailureFieldClearingTest(TestCase):
    """A38 — stale OR key/secret refs cleared when teardown raises."""

    def setUp(self):
        os.environ["AZURE_MOCK"] = "true"
        self.tenant = create_tenant(
            display_name="A38 Partial Deprovision Test",
            telegram_chat_id=438438438,
        )

    def tearDown(self):
        os.environ.pop("AZURE_MOCK", None)

    @override_settings(OPENROUTER_PER_TENANT_KEYS_ENABLED=True)
    @patch("apps.byo_models.services._delete_secret_from_kv")
    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.byo_models.services._write_secret_to_kv")
    @patch("apps.billing.openrouter_admin.create_sub_key")
    @patch("apps.orchestrator.services.delete_container_app")
    def test_or_fields_cleared_when_container_delete_raises(
        self,
        mock_delete_container,
        mock_create,
        mock_kv_write,
        mock_delete_sub,
        mock_kv_delete,
    ):
        """When delete_container_app raises, the except block must still clear
        openrouter_key_hash and openrouter_key_secret_name because the OR sub-key
        and KV secret deletes run (and are swallowed) before the container delete."""
        mock_create.return_value = ("or-key-abc", "hash-xyz")
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openrouter_key_hash, "hash-xyz")
        self.assertTrue(self.tenant.openrouter_key_secret_name)

        original_secret_name = self.tenant.openrouter_key_secret_name

        # Simulate a teardown failure mid-way through the container delete
        mock_delete_container.side_effect = RuntimeError("Azure 500: container delete timed out")

        with self.assertRaises(RuntimeError):
            deprovision_tenant(str(self.tenant.id))

        self.tenant.refresh_from_db()

        # Status lands on SUSPENDED (not DELETED) because teardown failed
        self.assertEqual(self.tenant.status, Tenant.Status.SUSPENDED)

        # OR key fields MUST be cleared — the sub-key and KV secret were
        # already deleted before the container delete call raised
        self.assertEqual(
            self.tenant.openrouter_key_hash,
            "",
            "openrouter_key_hash must be cleared in the except path to avoid "
            "referencing a deleted OR sub-key after reactivation",
        )
        self.assertEqual(
            self.tenant.openrouter_key_secret_name,
            "",
            "openrouter_key_secret_name must be cleared in the except path to avoid "
            "referencing a deleted KV secret after reactivation",
        )

        # OR/KV delete calls were still attempted
        mock_delete_sub.assert_called_once_with("hash-xyz")
        mock_kv_delete.assert_called_once_with(original_secret_name)

    @override_settings(OPENROUTER_PER_TENANT_KEYS_ENABLED=True)
    @patch("apps.byo_models.services._delete_secret_from_kv")
    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.byo_models.services._write_secret_to_kv")
    @patch("apps.billing.openrouter_admin.create_sub_key")
    @patch("apps.orchestrator.services.delete_tenant_file_share")
    def test_or_fields_cleared_when_file_share_delete_raises(
        self,
        mock_delete_fs,
        mock_create,
        mock_kv_write,
        mock_delete_sub,
        mock_kv_delete,
    ):
        """Same guarantee when the file-share delete (not the container delete) raises."""
        mock_create.return_value = ("or-key-def", "hash-qrs")
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()
        original_secret_name = self.tenant.openrouter_key_secret_name

        mock_delete_fs.side_effect = RuntimeError("Azure 500: file share delete failed")

        with self.assertRaises(RuntimeError):
            deprovision_tenant(str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.SUSPENDED)
        self.assertEqual(self.tenant.openrouter_key_hash, "")
        self.assertEqual(self.tenant.openrouter_key_secret_name, "")

        mock_delete_sub.assert_called_once_with("hash-qrs")
        mock_kv_delete.assert_called_once_with(original_secret_name)
