"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.core.credentials import AccessToken
from azure.keyvault.secrets import SecretClient
from azure.keyvault.secrets._models import KeyVaultSecret
from django.test import SimpleTestCase


class _DummyCredential:
    def get_token(self, *_scopes, **_kwargs):
        return AccessToken("offline-contract-test", 4_102_444_800)


class AzureKeyVaultSecretsSdkContractTest(SimpleTestCase):
    def test_client_and_method_signatures_accept_our_calls(self):
        client = SecretClient(vault_url="https://example.vault.azure.net", credential=_DummyCredential())

        inspect.signature(client.set_secret).bind("secret-name", "secret-value")
        inspect.signature(client.get_secret).bind("secret-name")
        inspect.signature(client.begin_delete_secret).bind("secret-name")

    def test_secret_value_attribute_exists_on_real_model(self):
        secret = KeyVaultSecret(properties=None, value="secret-value")

        self.assertEqual(secret.value, "secret-value")
