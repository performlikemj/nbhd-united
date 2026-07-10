"""Tests for apps.crypto.keys — DEK mint/wrap/unwrap service (Phase 1 PR2).

Runs against the stateful `_MOCK_KEK_REGISTRY` in
`apps.orchestrator.azure_client`. Mock mode is forced EXPLICITLY via a
class-level ``@patch(... _is_mock, return_value=True)`` — never via the
ambient ``AZURE_MOCK`` env var, which another test in CI's full suite can
mutate out from under us (mirrors PR1's merged test_azure_client_keys.py,
which is green in CI). Each test mints its own tenant(s) with a fresh random
UUID, and tearDown clears the module-level registry, so entries never leak
across tests.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.crypto.keys import get_wrapped_dek, mint_and_wrap_dek, unwrap_dek_for
from apps.orchestrator import azure_client
from apps.orchestrator.azure_client import _MOCK_KEK_REGISTRY
from apps.tenants.models import Tenant, TenantDek, User


def _create_tenant(*, suffix: str) -> Tenant:
    user = User.objects.create_user(username=f"crypto-keys-{suffix}", password="pass1234")
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, model_tier=Tenant.ModelTier.STARTER)


@patch("apps.orchestrator.azure_client._is_mock", return_value=True)
class MintAndWrapDekTest(TestCase):
    def tearDown(self):
        _MOCK_KEK_REGISTRY.clear()

    def test_mint_creates_kek_and_epoch_zero_row(self, _mock_is_mock):
        tenant = _create_tenant(suffix="mint")

        row = mint_and_wrap_dek(tenant)

        self.assertIsInstance(row, TenantDek)
        self.assertEqual(row.tenant_id, tenant.id)
        self.assertEqual(row.dek_epoch, 0)
        self.assertEqual(row.kek_version, "mock-v1")
        self.assertTrue(bytes(row.wrapped_dek))
        self.assertEqual(TenantDek.objects.filter(tenant=tenant).count(), 1)
        # KEK actually minted in the (mock) vault.
        self.assertIn(str(tenant.id), _MOCK_KEK_REGISTRY)

    def test_second_mint_is_a_no_op_returning_same_row(self, _mock_is_mock):
        tenant = _create_tenant(suffix="idempotent")

        first = mint_and_wrap_dek(tenant)
        second = mint_and_wrap_dek(tenant)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(bytes(first.wrapped_dek), bytes(second.wrapped_dek))
        self.assertEqual(TenantDek.objects.filter(tenant=tenant).count(), 1)

    def test_reprovision_within_grace_recovers_kek_and_reuses_row(self, _mock_is_mock):
        """A cancelled subscriber re-provisioned INSIDE the KEK grace window:
        the soft-deleted KEK is recovered and the same DEK row is reused, so
        the tenant comes back with its data decryptable."""
        tenant = _create_tenant(suffix="recover")
        first = mint_and_wrap_dek(tenant)

        # Deprovision soft-deletes the KEK but keeps the DEK row.
        azure_client.begin_delete_kek(tenant.id)
        # While soft-deleted, the DEK cannot unwrap (crypto ops disabled).
        with self.assertRaises(LookupError):
            unwrap_dek_for(tenant)

        second = mint_and_wrap_dek(tenant)

        # Same row, same wrapped material — no re-key happened.
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(bytes(first.wrapped_dek), bytes(second.wrapped_dek))
        self.assertEqual(TenantDek.objects.filter(tenant=tenant).count(), 1)
        # Recovered KEK -> the ORIGINAL DEK unwraps again.
        self.assertEqual(len(unwrap_dek_for(tenant)), 32)

    def test_reprovision_after_purge_fresh_starts_new_epoch0(self, _mock_is_mock):
        """A tenant re-provisioned AFTER the grace window (KEK purged): the
        prior ciphertext is cryptographically shredded, so the stale DEK row is
        dropped and a fresh epoch-0 DEK + KEK is minted."""
        tenant = _create_tenant(suffix="fresh")
        first = mint_and_wrap_dek(tenant)
        first_pk = first.pk
        first_wrapped = bytes(first.wrapped_dek)

        # Deprovision + post-grace purge (crypto-shred).
        azure_client.begin_delete_kek(tenant.id)
        azure_client.purge_kek(tenant.id)

        second = mint_and_wrap_dek(tenant)

        # Stale row gone; brand-new epoch-0 row with fresh key material.
        self.assertNotEqual(first_pk, second.pk)
        self.assertFalse(TenantDek.objects.filter(pk=first_pk).exists())
        self.assertEqual(second.dek_epoch, 0)
        self.assertNotEqual(bytes(second.wrapped_dek), first_wrapped)
        self.assertEqual(TenantDek.objects.filter(tenant=tenant).count(), 1)
        # The fresh DEK unwraps under the fresh KEK.
        self.assertEqual(len(unwrap_dek_for(tenant)), 32)


@patch("apps.orchestrator.azure_client._is_mock", return_value=True)
class GetWrappedDekTest(TestCase):
    def tearDown(self):
        _MOCK_KEK_REGISTRY.clear()

    def test_returns_wrapped_dek_and_kek_version(self, _mock_is_mock):
        tenant = _create_tenant(suffix="get-wrapped")
        minted = mint_and_wrap_dek(tenant)

        wrapped, kek_version = get_wrapped_dek(tenant)

        self.assertEqual(wrapped, bytes(minted.wrapped_dek))
        self.assertEqual(kek_version, minted.kek_version)

    def test_raises_when_no_dek_minted(self, _mock_is_mock):
        tenant = _create_tenant(suffix="unminted")

        with self.assertRaises(TenantDek.DoesNotExist):
            get_wrapped_dek(tenant)


@patch("apps.orchestrator.azure_client._is_mock", return_value=True)
class UnwrapDekForTest(TestCase):
    def tearDown(self):
        _MOCK_KEK_REGISTRY.clear()

    def test_round_trips_the_dek(self, _mock_is_mock):
        tenant = _create_tenant(suffix="roundtrip")
        mint_and_wrap_dek(tenant)

        dek = unwrap_dek_for(tenant)

        self.assertEqual(len(dek), 32)

    def test_raises_when_no_dek_minted(self, _mock_is_mock):
        tenant = _create_tenant(suffix="roundtrip-unminted")

        with self.assertRaises(TenantDek.DoesNotExist):
            unwrap_dek_for(tenant)

    def test_cross_tenant_isolation(self, _mock_is_mock):
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
