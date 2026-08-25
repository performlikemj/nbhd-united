"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.core.credentials import AccessToken
from azure.mgmt.msi import ManagedServiceIdentityClient
from azure.mgmt.msi.models import Identity
from django.test import SimpleTestCase


class _DummyCredential:
    def get_token(self, *_scopes, **_kwargs):
        return AccessToken("offline-contract-test", 4_102_444_800)


class AzureManagedIdentitySdkContractTest(SimpleTestCase):
    def test_identity_operation_signatures_accept_our_calls(self):
        client = ManagedServiceIdentityClient(_DummyCredential(), "subscription-id")
        operations = client.user_assigned_identities
        parameters = {"location": "japaneast", "tags": {"service": "nbhd-united"}}

        inspect.signature(operations.create_or_update).bind(
            resource_group_name="resource-group",
            resource_name="identity",
            parameters=parameters,
        )
        inspect.signature(operations.delete).bind(
            resource_group_name="resource-group",
            resource_name="identity",
        )

    def test_identity_result_attributes_exist(self):
        identity = Identity.deserialize(
            {
                "location": "japaneast",
                "id": "identity-id",
                "clientId": "client-id",
                "principalId": "principal-id",
            }
        )

        self.assertEqual(
            (identity.id, identity.client_id, identity.principal_id), ("identity-id", "client-id", "principal-id")
        )
