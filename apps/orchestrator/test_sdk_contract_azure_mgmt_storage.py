"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.core.credentials import AccessToken
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.storage.models import StorageAccountListKeysResult
from django.test import SimpleTestCase


class _DummyCredential:
    def get_token(self, *_scopes, **_kwargs):
        return AccessToken("offline-contract-test", 4_102_444_800)


class AzureStorageSdkShapeTest(SimpleTestCase):
    def test_list_keys_result_keys_is_a_list_field(self):
        result = StorageAccountListKeysResult.deserialize({"keys": [{"keyName": "key1", "value": "x"}]})

        self.assertFalse(callable(result.keys))
        self.assertEqual(result.keys[0].value, "x")

    def test_management_operation_signatures_accept_our_calls(self):
        client = StorageManagementClient(_DummyCredential(), "subscription-id")

        inspect.signature(client.storage_accounts.list_keys).bind("resource-group", "account")
        inspect.signature(client.file_shares.create).bind(
            resource_group_name="resource-group",
            account_name="account",
            share_name="share",
            file_share={},
        )
        inspect.signature(client.file_shares.delete).bind(
            resource_group_name="resource-group",
            account_name="account",
            share_name="share",
        )

    def test_caught_azure_core_exceptions_still_exist(self):
        self.assertTrue(issubclass(ResourceNotFoundError, Exception))
        self.assertTrue(issubclass(ResourceExistsError, Exception))
