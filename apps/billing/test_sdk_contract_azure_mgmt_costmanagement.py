"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.core.credentials import AccessToken
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import QueryResult
from django.test import SimpleTestCase


class _DummyCredential:
    def get_token(self, *_scopes, **_kwargs):
        return AccessToken("offline-contract-test", 4_102_444_800)


class AzureCostManagementSdkContractTest(SimpleTestCase):
    def test_query_usage_signature_accepts_our_dict_parameters(self):
        client = CostManagementClient(_DummyCredential())

        inspect.signature(client.query.usage).bind(
            scope="/subscriptions/subscription-id/resourceGroups/resource-group",
            parameters={"type": "Usage", "timeframe": "Custom", "dataset": {}},
        )

    def test_query_result_rows_attribute_exists(self):
        result = QueryResult(rows=[["1.25", "/subscriptions/x/resources/container"]])

        self.assertEqual(result.rows[0][0], "1.25")
