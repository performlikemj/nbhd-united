"""Tests for `backfill_tenant_deks` management command (Encryption-at-rest Phase 1 PR5)."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.crypto.keys import mint_and_wrap_dek
from apps.orchestrator.management.commands.backfill_tenant_deks import Command
from apps.tenants.models import Tenant, TenantDek
from apps.tenants.services import create_tenant


class BackfillTenantDeksTest(TestCase):
    def setUp(self):
        # Force the azure_client mock branch for the whole test (setUp AND
        # the test body). A class-level @patch decorator would NOT cover
        # setUp — and setUp calls mint_and_wrap_dek directly below — so we
        # start the patcher here and addCleanup it. Do NOT rely on the
        # ambient AZURE_MOCK env var: CI's full-suite run pollutes
        # os.environ, `_is_mock()` can flip False, and mint_and_wrap_dek ->
        # create_tenant_kek would then reach for REAL Key Vault. Matches the
        # intent of the merged PR1 test (test_azure_client_keys.py), which
        # patches _is_mock rather than trusting the env.
        patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Candidate: ACTIVE, no TenantDek row yet.
        self.tenant_active_no_dek = create_tenant(display_name="Active No DEK", telegram_chat_id=700101)
        self.tenant_active_no_dek.status = Tenant.Status.ACTIVE
        self.tenant_active_no_dek.save(update_fields=["status", "updated_at"])

        # Candidate: SUSPENDED, no TenantDek row yet — a suspended tenant's
        # data is still live and still needs a DEK for future encryption
        # phases.
        self.tenant_suspended_no_dek = create_tenant(display_name="Suspended No DEK", telegram_chat_id=700102)
        self.tenant_suspended_no_dek.status = Tenant.Status.SUSPENDED
        self.tenant_suspended_no_dek.save(update_fields=["status", "updated_at"])

        # Not a candidate: ACTIVE but already has a DEK row.
        self.tenant_already_keyed = create_tenant(display_name="Already Keyed", telegram_chat_id=700103)
        self.tenant_already_keyed.status = Tenant.Status.ACTIVE
        self.tenant_already_keyed.save(update_fields=["status", "updated_at"])
        mint_and_wrap_dek(self.tenant_already_keyed)

        # Not a candidate: still PENDING (never provisioned).
        self.tenant_pending = create_tenant(display_name="Pending", telegram_chat_id=700104)

        # Not a candidate: DEPROVISIONING (on the way out).
        self.tenant_deprovisioning = create_tenant(display_name="Deprovisioning", telegram_chat_id=700105)
        self.tenant_deprovisioning.status = Tenant.Status.DEPROVISIONING
        self.tenant_deprovisioning.save(update_fields=["status", "updated_at"])

    def test_candidate_filter(self):
        candidates = Command()._candidates(None)
        ids = {str(c.id) for c in candidates}

        self.assertIn(str(self.tenant_active_no_dek.id), ids)
        self.assertIn(str(self.tenant_suspended_no_dek.id), ids)
        self.assertNotIn(str(self.tenant_already_keyed.id), ids)
        self.assertNotIn(str(self.tenant_pending.id), ids)
        self.assertNotIn(str(self.tenant_deprovisioning.id), ids)

    def test_tenant_id_filter_targets_one_tenant(self):
        candidates = Command()._candidates(str(self.tenant_active_no_dek.id))
        ids = {str(c.id) for c in candidates}
        self.assertEqual(ids, {str(self.tenant_active_no_dek.id)})

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("backfill_tenant_deks", "--dry-run", stdout=out)

        self.assertFalse(TenantDek.objects.filter(tenant=self.tenant_active_no_dek).exists())
        self.assertFalse(TenantDek.objects.filter(tenant=self.tenant_suspended_no_dek).exists())
        self.assertIn("dry-run", out.getvalue())

    def test_backfill_mints_for_all_rowless_provisioned_tenants(self):
        out = StringIO()
        call_command("backfill_tenant_deks", stdout=out)

        self.assertTrue(TenantDek.objects.filter(tenant=self.tenant_active_no_dek, dek_epoch=0).exists())
        self.assertTrue(TenantDek.objects.filter(tenant=self.tenant_suspended_no_dek, dek_epoch=0).exists())
        # Untouched candidates stay untouched.
        self.assertEqual(TenantDek.objects.filter(tenant=self.tenant_already_keyed).count(), 1)
        self.assertFalse(TenantDek.objects.filter(tenant=self.tenant_pending).exists())
        self.assertFalse(TenantDek.objects.filter(tenant=self.tenant_deprovisioning).exists())
        self.assertIn("Minted: 2, Failed: 0", out.getvalue())

    def test_rerun_is_a_noop(self):
        call_command("backfill_tenant_deks")
        first_wrapped = bytes(TenantDek.objects.get(tenant=self.tenant_active_no_dek).wrapped_dek)

        out = StringIO()
        call_command("backfill_tenant_deks", stdout=out)

        self.assertEqual(TenantDek.objects.filter(tenant=self.tenant_active_no_dek).count(), 1)
        second_wrapped = bytes(TenantDek.objects.get(tenant=self.tenant_active_no_dek).wrapped_dek)
        self.assertEqual(first_wrapped, second_wrapped)
        self.assertIn("Found 0 tenant(s) needing a DEK", out.getvalue())
        self.assertIn("Minted: 0, Failed: 0", out.getvalue())

    def test_max_limits_candidate_count(self):
        out = StringIO()
        call_command("backfill_tenant_deks", "--max", "1", stdout=out)

        minted_count = TenantDek.objects.filter(
            tenant__in=[self.tenant_active_no_dek, self.tenant_suspended_no_dek]
        ).count()
        self.assertEqual(minted_count, 1)
        self.assertIn("Minted: 1, Failed: 0", out.getvalue())


class BackfillTenantDeksTaskTest(TestCase):
    """Tests for the QStash-triggered zero-arg task wrappers (Phase 1 activation).

    These fire the ``backfill_tenant_deks`` command from inside the running
    container via ``/api/cron/trigger/<name>/`` — see the TASK_MAP entries in
    apps/cron/views.py and the wrappers in apps/orchestrator/tasks.py.
    """

    def setUp(self):
        # Same rationale as BackfillTenantDeksTest.setUp: patch _is_mock rather
        # than trusting the ambient AZURE_MOCK env var, so the crypto layer uses
        # its stateful mock KEK/DEK vault instead of reaching real Key Vault.
        patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.tenant = create_tenant(display_name="Active No DEK (task)", telegram_chat_id=700201)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["status", "updated_at"])

    def test_dry_run_task_reports_candidates_and_writes_nothing(self):
        from apps.orchestrator.tasks import backfill_tenant_deks_dry_run_task

        result = backfill_tenant_deks_dry_run_task()

        self.assertIn("output", result)
        # Candidate count line from the command, plus the dry-run marker.
        self.assertIn("Found 1 tenant(s) needing a DEK", result["output"])
        self.assertIn("dry-run", result["output"])
        # No rows minted.
        self.assertFalse(TenantDek.objects.filter(tenant=self.tenant).exists())

    def test_real_task_mints_epoch_zero_dek(self):
        from apps.orchestrator.tasks import backfill_tenant_deks_task

        result = backfill_tenant_deks_task()

        self.assertIn("output", result)
        self.assertIn("Minted: 1, Failed: 0", result["output"])
        self.assertTrue(TenantDek.objects.filter(tenant=self.tenant, dek_epoch=0).exists())

    def test_task_map_names_resolve_zero_arg(self):
        """Both new names resolve through the same import path trigger_task uses.

        ``trigger_task`` resolves a TASK_MAP entry via ``import_module`` +
        ``getattr`` (see execute_task_sync / _validate_task_signature) and binds
        the request args. Mirror that here and assert each is a zero-arg callable
        — the contract that lets a no-body QStash publish drive it.
        """
        import inspect
        from importlib import import_module

        from apps.cron.views import TASK_MAP

        for name in (
            "backfill_tenant_deks",
            "backfill_tenant_deks_dry_run",
            "crypto_roundtrip_smoke",
            "encrypt_chat_history",
            "encrypt_chat_history_dry_run",
        ):
            self.assertIn(name, TASK_MAP)
            module_path, func_name = TASK_MAP[name].rsplit(".", 1)
            func = getattr(import_module(module_path), func_name)
            self.assertTrue(callable(func))
            # Binds with no positional/keyword args — the no-body contract.
            inspect.signature(func).bind()
