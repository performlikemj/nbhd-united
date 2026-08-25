"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.core.credentials import AccessToken
from azure.keyvault.keys import KeyClient
from azure.keyvault.keys.crypto import KeyWrapAlgorithm
from django.test import SimpleTestCase


class _DummyCredential:
    def get_token(self, *_scopes, **_kwargs):
        return AccessToken("offline-contract-test", 4_102_444_800)


class AzureKeyVaultKeysSdkContractTest(SimpleTestCase):
    def test_key_lifecycle_signatures_accept_our_calls(self):
        client = KeyClient(vault_url="https://example.vault.azure.net", credential=_DummyCredential())

        inspect.signature(client.create_rsa_key).bind("kek-tenant", size=3072)
        inspect.signature(client.get_cryptography_client).bind("kek-tenant")
        inspect.signature(client.begin_delete_key).bind("kek-tenant")
        inspect.signature(client.begin_recover_deleted_key).bind("kek-tenant")
        inspect.signature(client.get_key).bind("kek-tenant")
        inspect.signature(client.get_deleted_key).bind("kek-tenant")
        inspect.signature(client.purge_deleted_key).bind("kek-tenant")

    def test_wrap_algorithm_and_crypto_result_attributes_exist(self):
        from azure.keyvault.keys.crypto._models import UnwrapResult, WrapResult

        wrapped = WrapResult(
            key_id="https://example/keys/kek/version",
            algorithm=KeyWrapAlgorithm.rsa_oaep_256,
            encrypted_key=b"x",
        )
        unwrapped = UnwrapResult(
            key_id="https://example/keys/kek/version",
            algorithm=KeyWrapAlgorithm.rsa_oaep_256,
            key=b"x",
        )

        self.assertEqual(KeyWrapAlgorithm.rsa_oaep_256.value, "RSA-OAEP-256")
        self.assertEqual(wrapped.encrypted_key, b"x")
        self.assertEqual(wrapped.key_id.rsplit("/", 1)[-1], "version")
        self.assertEqual(unwrapped.key, b"x")
