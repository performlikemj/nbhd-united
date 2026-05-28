"""Tests for backfill_openrouter_keys + sweep_orphan_openrouter_keys
management commands (PR #1.6 Phase 5)."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


@override_settings(OPENROUTER_PER_TENANT_KEYS_ENABLED=True)
class BackfillTest(TestCase):
    def setUp(self):
        self.t1 = create_tenant(display_name="Backfill Test 1", telegram_chat_id=901)
        self.t1.status = Tenant.Status.ACTIVE
        self.t1.container_id = "oc-test-1"
        self.t1.managed_identity_id = "/subscriptions/x/identity"
        self.t1.save()

        self.t2 = create_tenant(display_name="Backfill Test 2", telegram_chat_id=902)
        self.t2.status = Tenant.Status.ACTIVE
        self.t2.container_id = "oc-test-2"
        self.t2.managed_identity_id = "/subscriptions/x/identity-2"
        # Already has a sub-key — must be skipped.
        self.t2.openrouter_key_secret_name = "tenants-2-openrouter-key"
        self.t2.openrouter_key_hash = "existing-hash"
        self.t2.save()

        self.t3 = create_tenant(display_name="Backfill Test 3", telegram_chat_id=903)
        self.t3.status = Tenant.Status.SUSPENDED  # eligible, but no container update
        self.t3.save()

        self.t4 = create_tenant(display_name="Backfill Test 4", telegram_chat_id=904)
        self.t4.status = Tenant.Status.PENDING  # ineligible
        self.t4.save()

    def test_flag_off_refuses_to_run(self):
        with override_settings(OPENROUTER_PER_TENANT_KEYS_ENABLED=False), self.assertRaises(CommandError):
            call_command("backfill_openrouter_keys")

    @patch("apps.orchestrator.azure_client.update_container_openrouter_key_secret")
    @patch("apps.orchestrator.azure_client.assign_key_vault_role")
    @patch("apps.orchestrator.azure_client.get_identity_client")
    @patch("apps.byo_models.services._write_secret_to_kv")
    @patch("apps.billing.openrouter_admin.create_sub_key")
    def test_creates_keys_only_for_eligible_tenants(self, mock_create, mock_kv, mock_idc, mock_grant, mock_rebind):
        # Identity-client stub: principal_id read from the MI lookup feeds
        # into assign_key_vault_role. Just needs to return SOMETHING with
        # a .principal_id attr.
        mock_idc.return_value.user_assigned_identities.get.return_value.principal_id = "test-principal"
        mock_create.side_effect = [
            ("mock-or-key-t1", "hash-t1"),
            ("mock-or-key-t3", "hash-t3"),
        ]
        out = StringIO()
        call_command("backfill_openrouter_keys", stdout=out)

        # t1 and t3 got sub-keys; t2 skipped (already had one); t4 skipped (PENDING).
        self.assertEqual(mock_create.call_count, 2)
        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.t3.refresh_from_db()
        self.t4.refresh_from_db()

        self.assertEqual(self.t1.openrouter_key_hash, "hash-t1")
        self.assertEqual(self.t2.openrouter_key_hash, "existing-hash")  # untouched
        self.assertEqual(self.t3.openrouter_key_hash, "hash-t3")
        self.assertEqual(self.t4.openrouter_key_hash, "")

        # KV role granted only for t1 — t3 has no managed_identity_id in
        # setUp ("eligible, but no container update") so the grant is
        # skipped (would be a no-op anyway with no MI to grant to).
        self.assertEqual(mock_grant.call_count, 1)
        # Only ACTIVE tenants get a container rebind. t3 is SUSPENDED → skipped.
        self.assertEqual(mock_rebind.call_count, 1)

    @patch("apps.billing.openrouter_admin.create_sub_key")
    def test_dry_run_makes_no_changes(self, mock_create):
        out = StringIO()
        call_command("backfill_openrouter_keys", "--dry-run", stdout=out)
        mock_create.assert_not_called()
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.openrouter_key_secret_name, "")

    @patch("apps.orchestrator.azure_client.update_container_openrouter_key_secret")
    @patch("apps.orchestrator.azure_client.assign_key_vault_role")
    @patch("apps.orchestrator.azure_client.get_identity_client")
    @patch("apps.byo_models.services._write_secret_to_kv")
    @patch("apps.billing.openrouter_admin.create_sub_key")
    def test_filter_by_single_tenant(self, mock_create, mock_kv, mock_idc, mock_grant, mock_rebind):
        mock_idc.return_value.user_assigned_identities.get.return_value.principal_id = "test-principal"
        mock_create.return_value = ("mock-or-key-only", "hash-only")
        out = StringIO()
        call_command("backfill_openrouter_keys", f"--tenant={self.t1.id}", stdout=out)

        self.assertEqual(mock_create.call_count, 1)
        self.t1.refresh_from_db()
        self.t3.refresh_from_db()
        self.assertEqual(self.t1.openrouter_key_hash, "hash-only")
        self.assertEqual(self.t3.openrouter_key_hash, "")


class SweepTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sweep Test", telegram_chat_id=905)
        self.tenant.openrouter_key_hash = "known-hash"
        self.tenant.save()

    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.billing.openrouter_admin.list_sub_keys")
    def test_deletes_old_orphans_keeps_known_keys(self, mock_list, mock_delete):
        # Three OR keys: one tied to our tenant, one orphan + old, one orphan + young.
        mock_list.return_value = [
            {"hash": "known-hash", "label": "tenant-1", "created_at": "2026-04-01T00:00:00Z"},
            {"hash": "orphan-old", "label": "tenant-old", "created_at": "2026-04-01T00:00:00Z"},
            {"hash": "orphan-young", "label": "tenant-young", "created_at": "2099-01-01T00:00:00Z"},
        ]
        out = StringIO()
        call_command("sweep_orphan_openrouter_keys", stdout=out)

        # Only the old orphan should have been deleted.
        mock_delete.assert_called_once_with("orphan-old")

    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.billing.openrouter_admin.list_sub_keys")
    def test_dry_run_does_not_delete(self, mock_list, mock_delete):
        mock_list.return_value = [
            {"hash": "orphan-old", "label": "x", "created_at": "2026-01-01T00:00:00Z"},
        ]
        out = StringIO()
        call_command("sweep_orphan_openrouter_keys", "--dry-run", stdout=out)
        mock_delete.assert_not_called()

    @patch("apps.billing.openrouter_admin.delete_sub_key")
    @patch("apps.billing.openrouter_admin.list_sub_keys")
    def test_skips_when_no_created_at(self, mock_list, mock_delete):
        # Missing created_at → treated as young (conservative).
        mock_list.return_value = [{"hash": "orphan-no-ts", "label": "x"}]
        out = StringIO()
        call_command("sweep_orphan_openrouter_keys", stdout=out)
        mock_delete.assert_not_called()
