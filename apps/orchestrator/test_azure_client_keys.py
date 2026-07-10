"""Tests for the Azure KEK SDK layer (encryption-at-rest, Phase 1 / PR1).

Everything here runs against the stateful ``_MOCK_KEK_REGISTRY`` — these
functions have zero real callers yet (built DARK; nothing reads ciphertext
until later PRs), so the mock IS the contract under test. The one thing a
deterministic/derived-key mock could never prove is the T4 shred invariant
(purge -> unwrap raises), which is why the mock is stateful instead of a
pure function of tenant_id.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

import apps.orchestrator.azure_client as azure_client
from apps.orchestrator.azure_client import (
    _MOCK_KEK_REGISTRY,
    _get_decrypt_broker_credential,
    _get_provisioner_credential,
    begin_delete_kek,
    create_tenant_kek,
    kek_liveness,
    purge_kek,
    recover_kek,
    unwrap_dek,
    wrap_dek,
)


def _fresh_tenant_id() -> str:
    """A unique tenant id per test so tests never share `_MOCK_KEK_REGISTRY`
    state (it's module-level and mutated in place)."""
    return str(uuid.uuid4())


@patch("apps.orchestrator.azure_client._is_mock", return_value=True)
class MockKekLifecycleTest(SimpleTestCase):
    def tearDown(self):
        # Belt-and-suspenders: even though each test uses a unique tenant
        # id, don't let a failed assertion mid-test leak an entry forward.
        _MOCK_KEK_REGISTRY.clear()

    def test_wrap_unwrap_round_trips_32_byte_dek(self, _mock_is_mock):
        tenant_id = _fresh_tenant_id()
        dek = os.urandom(32)

        create_tenant_kek(tenant_id)
        wrapped, kek_version = wrap_dek(tenant_id, dek)

        self.assertIsInstance(wrapped, bytes)
        self.assertNotEqual(wrapped, dek)  # actually wrapped, not passed through
        self.assertEqual(kek_version, "mock-v1")

        unwrapped = unwrap_dek(tenant_id, wrapped)
        self.assertEqual(unwrapped, dek)

    def test_wrap_on_unminted_tenant_raises(self, _mock_is_mock):
        tenant_id = _fresh_tenant_id()
        with self.assertRaises(LookupError):
            wrap_dek(tenant_id, os.urandom(32))

    def test_unwrap_on_unminted_tenant_raises(self, _mock_is_mock):
        tenant_id = _fresh_tenant_id()
        with self.assertRaises(LookupError):
            unwrap_dek(tenant_id, b"whatever")

    def test_begin_delete_kek_disables_unwrap(self, _mock_is_mock):
        """Soft-delete must break unwrap IMMEDIATELY — real Key Vault disables
        crypto ops the instant a key is deleted. The recovery window preserves
        recoverability (`recover_kek`), not usability."""
        tenant_id = _fresh_tenant_id()
        dek = os.urandom(32)
        create_tenant_kek(tenant_id)
        wrapped, _ = wrap_dek(tenant_id, dek)

        begin_delete_kek(tenant_id)

        with self.assertRaises(LookupError):
            unwrap_dek(tenant_id, wrapped)

    def test_recover_kek_restores_unwrap_after_soft_delete(self, _mock_is_mock):
        """Grace-window resurrection: recover a soft-deleted KEK and the
        original DEK unwraps again under the SAME key material (this is what
        lets an in-grace re-provision keep its data)."""
        tenant_id = _fresh_tenant_id()
        dek = os.urandom(32)
        create_tenant_kek(tenant_id)
        wrapped, _ = wrap_dek(tenant_id, dek)
        begin_delete_kek(tenant_id)

        recover_kek(tenant_id)

        self.assertEqual(unwrap_dek(tenant_id, wrapped), dek)

    def test_recover_of_purged_kek_raises(self, _mock_is_mock):
        """A purged key is gone for good — recover must fail loudly, never
        silently re-mint (that would fabricate a key, not restore one)."""
        tenant_id = _fresh_tenant_id()
        create_tenant_kek(tenant_id)
        begin_delete_kek(tenant_id)
        purge_kek(tenant_id)

        with self.assertRaises(LookupError):
            recover_kek(tenant_id)

    def test_kek_liveness_tracks_the_full_lifecycle(self, _mock_is_mock):
        """`kek_liveness` must distinguish live / recoverable / absent across
        every transition — the re-provision path branches on it, and only a
        DEFINITIVE absent is allowed to trigger a destructive re-key."""
        tenant_id = _fresh_tenant_id()
        self.assertEqual(kek_liveness(tenant_id), "absent")  # never minted

        create_tenant_kek(tenant_id)
        self.assertEqual(kek_liveness(tenant_id), "live")

        begin_delete_kek(tenant_id)
        self.assertEqual(kek_liveness(tenant_id), "recoverable")

        recover_kek(tenant_id)
        self.assertEqual(kek_liveness(tenant_id), "live")

        begin_delete_kek(tenant_id)
        purge_kek(tenant_id)
        self.assertEqual(kek_liveness(tenant_id), "absent")

    def test_create_delete_purge_then_unwrap_raises(self, _mock_is_mock):
        """The T4 shred invariant: create -> wrap/unwrap round-trip ->
        begin_delete (unwrap now disabled) -> purge -> unwrap RAISES for good.

        This is the one property a deterministic mock (derive-key-from-
        tenant-id) could never prove, since there'd be no state to destroy —
        the whole reason `_MOCK_KEK_REGISTRY` is stateful.
        """
        tenant_id = _fresh_tenant_id()
        dek = os.urandom(32)

        create_tenant_kek(tenant_id)
        wrapped, _ = wrap_dek(tenant_id, dek)
        self.assertEqual(unwrap_dek(tenant_id, wrapped), dek)

        begin_delete_kek(tenant_id)
        with self.assertRaises(LookupError):  # soft-delete disables crypto ops
            unwrap_dek(tenant_id, wrapped)

        purge_kek(tenant_id)

        with self.assertRaises(LookupError):
            unwrap_dek(tenant_id, wrapped)

    def test_purge_of_never_minted_tenant_is_a_noop(self, _mock_is_mock):
        tenant_id = _fresh_tenant_id()
        purge_kek(tenant_id)  # must not raise
        with self.assertRaises(LookupError):
            unwrap_dek(tenant_id, b"whatever")

    def test_two_tenants_have_independent_keys(self, _mock_is_mock):
        """A DEK wrapped under tenant A's KEK must not unwrap cleanly under
        tenant B's — cross-tenant key confusion would be a T3 violation."""
        tenant_a = _fresh_tenant_id()
        tenant_b = _fresh_tenant_id()
        dek = os.urandom(32)

        create_tenant_kek(tenant_a)
        create_tenant_kek(tenant_b)
        wrapped_under_a, _ = wrap_dek(tenant_a, dek)

        # unwrap_dek doesn't validate provenance by itself (that's AAD's job
        # in apps/crypto, PR3) — but XOR-under-the-wrong-key must not just
        # happen to reproduce the original plaintext.
        unwrapped_under_b = unwrap_dek(tenant_b, wrapped_under_a)
        self.assertNotEqual(unwrapped_under_b, dek)


class KekVaultConfigTest(SimpleTestCase):
    """Real (non-mock) path: every KEK function must refuse to proceed
    without AZURE_KEK_VAULT_NAME configured, matching the existing
    ``AZURE_KEY_VAULT_NAME`` guard pattern elsewhere in this module."""

    @override_settings(AZURE_KEK_VAULT_NAME="")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    def test_create_tenant_kek_raises_on_missing_vault_name(self, _mock_is_mock):
        with self.assertRaises(ValueError):
            create_tenant_kek("tenant-abc")

    @override_settings(AZURE_KEK_VAULT_NAME="")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    def test_wrap_dek_raises_on_missing_vault_name(self, _mock_is_mock):
        with self.assertRaises(ValueError):
            wrap_dek("tenant-abc", os.urandom(32))

    @override_settings(AZURE_KEK_VAULT_NAME="")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    def test_unwrap_dek_raises_on_missing_vault_name(self, _mock_is_mock):
        with self.assertRaises(ValueError):
            unwrap_dek("tenant-abc", b"wrapped")

    @override_settings(AZURE_KEK_VAULT_NAME="")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    def test_begin_delete_kek_raises_on_missing_vault_name(self, _mock_is_mock):
        with self.assertRaises(ValueError):
            begin_delete_kek("tenant-abc")

    @override_settings(AZURE_KEK_VAULT_NAME="")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    def test_purge_kek_raises_on_missing_vault_name(self, _mock_is_mock):
        with self.assertRaises(ValueError):
            purge_kek("tenant-abc")


class DecryptBrokerCredentialTest(SimpleTestCase):
    """The broker and provisioner credentials must be genuinely separate
    objects — never the same cached identity — or the whole point of the
    RBAC split (provisioner can't unwrap) is void in-process."""

    def setUp(self):
        # These getters cache into module globals; reset before AND after
        # so this test can't leak a stale credential into (or read one left
        # by) any other test module that imports azure_client.
        self._orig_provisioner = azure_client._provisioner_credential
        self._orig_broker = azure_client._decrypt_broker_credential
        azure_client._provisioner_credential = None
        azure_client._decrypt_broker_credential = None

    def tearDown(self):
        azure_client._provisioner_credential = self._orig_provisioner
        azure_client._decrypt_broker_credential = self._orig_broker

    @override_settings(
        AZURE_PROVISIONER_CLIENT_ID="mi-nbhd-provisioner",
        AZURE_DECRYPT_BROKER_CLIENT_ID="mi-nbhd-decrypt",
    )
    @patch("azure.identity.ManagedIdentityCredential")
    def test_broker_and_provisioner_use_distinct_managed_identities(self, mock_mi_cls):
        mock_mi_cls.side_effect = lambda client_id: SimpleNamespace(client_id=client_id, _tag="mi")

        provisioner_cred = _get_provisioner_credential()
        broker_cred = _get_decrypt_broker_credential()

        self.assertIsNot(provisioner_cred, broker_cred)
        self.assertEqual(provisioner_cred.client_id, "mi-nbhd-provisioner")
        self.assertEqual(broker_cred.client_id, "mi-nbhd-decrypt")

        # Cached: a second call returns the SAME object, not a fresh one.
        self.assertIs(_get_provisioner_credential(), provisioner_cred)
        self.assertIs(_get_decrypt_broker_credential(), broker_cred)
        self.assertEqual(mock_mi_cls.call_count, 2)

    @override_settings(AZURE_PROVISIONER_CLIENT_ID="", AZURE_DECRYPT_BROKER_CLIENT_ID="")
    @patch("azure.identity.DefaultAzureCredential")
    def test_local_dev_fallback_still_yields_distinct_objects(self, mock_default_cls):
        mock_default_cls.side_effect = lambda: SimpleNamespace(_tag="default")

        provisioner_cred = _get_provisioner_credential()
        broker_cred = _get_decrypt_broker_credential()

        self.assertIsNot(provisioner_cred, broker_cred)
        self.assertEqual(mock_default_cls.call_count, 2)
