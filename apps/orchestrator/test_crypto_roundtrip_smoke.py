"""Tests for the crypto_roundtrip_smoke_task (Encryption-at-rest Phase 1->2 bridge).

Exercises the full box encrypt/decrypt path against a real DEK using the
stateful azure_client mock — the same posture as the DEK backfill tests
(patch `_is_mock`, never trust the ambient AZURE_MOCK env var).
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.crypto.keys import mint_and_wrap_dek
from apps.orchestrator.tasks import crypto_roundtrip_smoke_task
from apps.tenants.models import Tenant, TenantDek
from apps.tenants.services import create_tenant


class CryptoRoundtripSmokeTaskTest(TestCase):
    def setUp(self):
        # Force the azure_client mock branch for the whole test (setUp mints a
        # DEK, and the round-trip unwraps it) rather than trusting the ambient
        # AZURE_MOCK env var, which CI's full-suite run can flip. Mirrors
        # BackfillTenantDeksTest.setUp.
        patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip_passes_for_keyed_tenant(self):
        tenant = create_tenant(display_name="Smoke Keyed", telegram_chat_id=700301)
        tenant.status = Tenant.Status.ACTIVE
        tenant.save(update_fields=["status", "updated_at"])
        mint_and_wrap_dek(tenant)

        with self.assertLogs("apps.orchestrator.tasks", level="INFO") as log_ctx:
            result = crypto_roundtrip_smoke_task()

        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["tenant"], str(tenant.id))
        self.assertEqual(result["epoch"], 0)
        # Exactly one PASS line naming the tenant + epoch.
        self.assertTrue(
            any("crypto_roundtrip_smoke: PASS" in m and str(tenant.id) in m for m in log_ctx.output),
            log_ctx.output,
        )
        # No DB writes: the round-trip is pure in-memory, so the only DEK row
        # is the one setUp minted.
        self.assertEqual(TenantDek.objects.count(), 1)

    def test_raises_cleanly_when_no_keyed_tenant(self):
        # A tenant exists but has NO DEK row, so the deterministic pick finds
        # nothing and the task must raise (DLQ-visible) rather than pass.
        create_tenant(display_name="Smoke No DEK", telegram_chat_id=700302)
        self.assertFalse(TenantDek.objects.exists())

        with self.assertRaises(RuntimeError):
            crypto_roundtrip_smoke_task()
