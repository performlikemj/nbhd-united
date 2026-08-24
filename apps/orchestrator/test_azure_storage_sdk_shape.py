"""Regression guard for the 2026-08-24 azure-mgmt-storage 25.x incident."""

from azure.mgmt.storage.models import StorageAccountListKeysResult
from django.test import SimpleTestCase


class AzureStorageSdkShapeTest(SimpleTestCase):
    def test_list_keys_result_keys_is_a_list_field(self):
        result = StorageAccountListKeysResult.deserialize({"keys": [{"keyName": "key1", "value": "x"}]})

        self.assertFalse(callable(result.keys))
        self.assertEqual(result.keys[0].value, "x")
