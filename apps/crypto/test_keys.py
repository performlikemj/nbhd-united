"""Tests for apps.crypto.keys — DEK mint/wrap/unwrap service (Phase 1 PR2).

Runs against the stateful `_MOCK_KEK_REGISTRY` in
`apps.orchestrator.azure_client` (AZURE_MOCK=true). Each test mints its own
tenant(s) with a fresh random UUID, so registry entries never collide across
tests within the same run.
"""

from __future__ import annotations

from django.test import TestCase

from apps.crypto.keys import get_wrapped_dek, mint_and_wrap_dek, unwrap_dek_for
from apps.orchestrator import azure_client
from apps.tenants.models import Tenant, TenantDek, User


def _create_tenant(*, suffix: str) -> Tenant:
    user = User.objects.create_user(username=f"crypto-keys-{suffix}", password="pass1234")
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, model_tier=Tenant.ModelTier.STARTER)


class MintAndWrapDekTest(TestCase):
    def test_mint_creates_kek_and_epoch_zero_row(self):
        tenant = _create_tenant(suffix="mint")

        row = mint_and_wrap_dek(tenant)

        self.assertIsInstance(row, TenantDek)
        self.assertEqual(row.tenant_id, tenant.id)
        self.assertEqual(row.dek_epoch, 0)
        self.assertEqual(row.kek_version, "mock-v1")
        self.assertTrue(bytes(row.wrapped_dek))
        self.assertEqual(TenantDek.objects.filter(tenant=tenant).count(), 1)

    def test_second_mint_is_a_no_op_returning_same_row(self):
        tenant = _create_tenant(suffix="idempotent")

        first = mint_and_wrap_dek(tenant)
        second = mint_and_wrap_dek(tenant)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(bytes(first.wrapped_dek), bytes(second.wrapped_dek))
        self.assertEqual(TenantDek.objects.filter(tenant=tenant).count(), 1)


class GetWrappedDekTest(TestCase):
    def test_returns_wrapped_dek_and_kek_version(self):
        tenant = _create_tenant(suffix="get-wrapped")
        minted = mint_and_wrap_dek(tenant)

        wrapped, kek_version = get_wrapped_dek(tenant)

        self.assertEqual(wrapped, bytes(minted.wrapped_dek))
        self.assertEqual(kek_version, minted.kek_version)

    def test_raises_when_no_dek_minted(self):
        tenant = _create_tenant(suffix="unminted")

        with self.assertRaises(TenantDek.DoesNotExist):
            get_wrapped_dek(tenant)


class UnwrapDekForTest(TestCase):
    def test_round_trips_the_dek(self):
        tenant = _create_tenant(suffix="roundtrip")
        mint_and_wrap_dek(tenant)

        dek = unwrap_dek_for(tenant)

        self.assertEqual(len(dek), 32)

    def test_raises_when_no_dek_minted(self):
        tenant = _create_tenant(suffix="roundtrip-unminted")

        with self.assertRaises(TenantDek.DoesNotExist):
            unwrap_dek_for(tenant)

    def test_cross_tenant_isolation(self):
        """Tenant A's wrapped DEK is meaningless under tenant B's KEK.

        Each mock tenant gets its own random KEK material, so unwrapping A's
        wrapped_dek bytes under B's tenant_id produces different (garbage)
        output rather than A's real plaintext DEK — proving a wrapped DEK is
        only ever valid paired with the tenant_id it was wrapped under.
        """
        tenant_a = _create_tenant(suffix="cross-a")
        tenant_b = _create_tenant(suffix="cross-b")
        dek_row_a = mint_and_wrap_dek(tenant_a)
        mint_and_wrap_dek(tenant_b)

        dek_a = unwrap_dek_for(tenant_a)
        dek_b = unwrap_dek_for(tenant_b)
        cross_unwrapped = azure_client.unwrap_dek(tenant_b.id, bytes(dek_row_a.wrapped_dek))

        self.assertNotEqual(dek_a, dek_b)
        self.assertNotEqual(cross_unwrapped, dek_a)
