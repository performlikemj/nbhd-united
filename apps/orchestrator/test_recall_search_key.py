"""Tests for per-tenant recall blind-index search keys."""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceNotFoundError
from django.test import SimpleTestCase

from apps.orchestrator import recall_search_key
from apps.orchestrator.recall_search_key import (
    begin_delete_recall_search_key,
    blind_index_tokens,
    get_or_mint_recall_search_key,
)


def _tenant():
    tenant_id = uuid.uuid4()
    return SimpleNamespace(id=tenant_id, key_vault_prefix=f"tenants-{tenant_id}")


@patch("apps.orchestrator.recall_search_key._is_mock", return_value=True)
class MockRecallSearchKeyTest(SimpleTestCase):
    def tearDown(self):
        recall_search_key._CACHE.clear()
        recall_search_key._MOCK_RECALL_SEARCH_KEY_REGISTRY.clear()

    def test_mints_once_then_uses_process_cache_and_mock_vault(self, _mock_is_mock):
        tenant = _tenant()
        fixed_key = bytes(range(32))

        with patch("apps.orchestrator.recall_search_key.os.urandom", return_value=fixed_key) as mock_random:
            first = get_or_mint_recall_search_key(tenant)
            second = get_or_mint_recall_search_key(tenant)

            # A new process would have an empty cache but must load the same KV
            # value rather than minting another key.
            recall_search_key._CACHE.clear()
            third = get_or_mint_recall_search_key(tenant)

        self.assertEqual(first, fixed_key)
        self.assertEqual(second, fixed_key)
        self.assertEqual(third, fixed_key)
        self.assertEqual(len(first), 32)
        mock_random.assert_called_once_with(32)

    def test_secret_name_uses_tenant_key_vault_prefix(self, _mock_is_mock):
        tenant = _tenant()
        get_or_mint_recall_search_key(tenant)

        self.assertEqual(
            list(recall_search_key._MOCK_RECALL_SEARCH_KEY_REGISTRY),
            [f"{tenant.key_vault_prefix}-recall-search-key"],
        )

    def test_soft_delete_evicts_then_recovers_the_same_key(self, _mock_is_mock):
        tenant = _tenant()
        original = get_or_mint_recall_search_key(tenant)

        begin_delete_recall_search_key(tenant)

        self.assertNotIn(str(tenant.id), recall_search_key._CACHE)
        entry = recall_search_key._MOCK_RECALL_SEARCH_KEY_REGISTRY[f"{tenant.key_vault_prefix}-recall-search-key"]
        self.assertIs(entry["deleted"], True)

        recovered = get_or_mint_recall_search_key(tenant)
        self.assertEqual(recovered, original)
        self.assertIs(entry["deleted"], False)

    def test_logs_never_contain_key_material(self, _mock_is_mock):
        tenant = _tenant()
        fixed_key = bytes(range(32))

        with (
            patch("apps.orchestrator.recall_search_key.os.urandom", return_value=fixed_key),
            self.assertLogs("apps.orchestrator.recall_search_key", level="INFO") as captured,
        ):
            get_or_mint_recall_search_key(tenant)

        output = "\n".join(captured.output)
        self.assertNotIn(fixed_key.hex(), output)
        self.assertNotIn(base64.b64encode(fixed_key).decode("ascii"), output)


@patch("apps.orchestrator.recall_search_key._is_mock", return_value=False)
class KeyVaultRecallSearchKeyTest(SimpleTestCase):
    def tearDown(self):
        recall_search_key._CACHE.clear()

    @patch("apps.orchestrator.recall_search_key._secret_client")
    def test_cold_miss_mints_and_stores_32_random_bytes(self, mock_client_factory, _mock_is_mock):
        tenant = _tenant()
        client = mock_client_factory.return_value
        client.get_secret.side_effect = ResourceNotFoundError()
        client.get_deleted_secret.side_effect = ResourceNotFoundError()
        fixed_key = bytes(range(32))

        with patch("apps.orchestrator.recall_search_key.os.urandom", return_value=fixed_key) as mock_random:
            result = get_or_mint_recall_search_key(tenant)

        self.assertEqual(result, fixed_key)
        mock_random.assert_called_once_with(32)
        client.set_secret.assert_called_once_with(
            f"{tenant.key_vault_prefix}-recall-search-key",
            base64.b64encode(fixed_key).decode("ascii"),
        )

    @patch("apps.orchestrator.recall_search_key._secret_client")
    def test_soft_deleted_secret_recovers_same_key_without_minting(self, mock_client_factory, _mock_is_mock):
        tenant = _tenant()
        client = mock_client_factory.return_value
        fixed_key = bytes(reversed(range(32)))
        active_secret = SimpleNamespace(value=base64.b64encode(fixed_key).decode("ascii"))
        client.get_secret.side_effect = [ResourceNotFoundError(), active_secret]
        recover_poller = MagicMock()
        client.begin_recover_deleted_secret.return_value = recover_poller

        with patch("apps.orchestrator.recall_search_key.os.urandom") as mock_random:
            result = get_or_mint_recall_search_key(tenant)

        self.assertEqual(result, fixed_key)
        client.get_deleted_secret.assert_called_once_with(f"{tenant.key_vault_prefix}-recall-search-key")
        client.begin_recover_deleted_secret.assert_called_once_with(f"{tenant.key_vault_prefix}-recall-search-key")
        recover_poller.wait.assert_called_once_with()
        mock_random.assert_not_called()

    @patch("apps.orchestrator.recall_search_key._secret_client")
    def test_delete_of_never_minted_key_is_idempotent(self, mock_client_factory, _mock_is_mock):
        tenant = _tenant()
        client = mock_client_factory.return_value
        client.begin_delete_secret.side_effect = ResourceNotFoundError()

        begin_delete_recall_search_key(tenant)

        client.begin_delete_secret.assert_called_once_with(f"{tenant.key_vault_prefix}-recall-search-key")


class BlindIndexTokensTest(SimpleTestCase):
    def test_fixed_vectors_case_folding_and_duplicate_preservation(self):
        key = bytes([11]) * 32

        tokens = blind_index_tokens(key, ["Heart", "HEART", "sleep", "sleep"])

        self.assertEqual(
            tokens,
            [
                "4275f31343093aef9c0bd4c3",
                "4275f31343093aef9c0bd4c3",
                "67451296e80cefcb678bf42e",
                "67451296e80cefcb678bf42e",
            ],
        )

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(blind_index_tokens(b"fixed-test-key", []), [])
