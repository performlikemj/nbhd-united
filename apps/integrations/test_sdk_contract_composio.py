"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from composio import Composio
from django.test import SimpleTestCase


class ComposioSdkContractTest(SimpleTestCase):
    def test_client_and_connected_account_api_shapes_exist(self):
        client = Composio(api_key="offline-key")

        self.assertTrue(hasattr(client, "connected_accounts"))
        inspect.signature(client.connected_accounts.initiate).bind(
            user_id="tenant-123",
            auth_config_id="auth-config",
            callback_url="https://example.test/callback",
            allow_multiple=True,
        )
        inspect.signature(client.connected_accounts.wait_for_connection).bind(id="request-id", timeout=30)
        inspect.signature(client.connected_accounts.get).bind("connected-account-id")

    def test_connected_account_response_models_keep_read_attributes(self):
        from composio_client.types.connected_account_retrieve_response import (
            ConnectedAccountRetrieveResponse,
        )

        fields = ConnectedAccountRetrieveResponse.model_fields
        self.assertIn("id", fields)
        self.assertIn("status", fields)
        self.assertIn("state", fields)
