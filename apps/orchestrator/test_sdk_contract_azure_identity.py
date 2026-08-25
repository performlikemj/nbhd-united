"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from django.test import SimpleTestCase


class AzureIdentitySdkContractTest(SimpleTestCase):
    def test_credential_constructors_accept_our_calls_without_authenticating(self):
        inspect.signature(DefaultAzureCredential).bind()
        inspect.signature(ManagedIdentityCredential).bind(client_id="managed-identity-client-id")

        default = DefaultAzureCredential()
        managed = ManagedIdentityCredential(client_id="managed-identity-client-id")
        self.assertTrue(callable(default.get_token))
        self.assertTrue(callable(managed.get_token))
