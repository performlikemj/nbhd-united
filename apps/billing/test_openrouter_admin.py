"""Tests for ``apps.billing.openrouter_admin`` — the OR management API
helpers backing per-tenant sub-key provisioning (PR #1.6).

These tests mock the HTTP layer (httpx) so they don't hit the real
OpenRouter API. Real-API behaviour is verified during canary rollout.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.billing.openrouter_admin import (
    OpenRouterAdminError,
    create_sub_key,
    delete_sub_key,
    get_key_usage,
    secret_name_for_tenant,
)


def _mock_response(status: int, json_body=None, text_body: str = ""):
    """Build a mock httpx response with status + body."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body if json_body is not None else {}
    resp.text = text_body or (str(json_body) if json_body is not None else "")
    return resp


@override_settings(
    AZURE_KV_SECRET_OPENROUTER_MANAGEMENT_KEY="openrouter-management-key",
    OPENROUTER_API_BASE="https://openrouter.ai/api/v1",
)
class CreateSubKeyTest(TestCase):
    @patch("apps.billing.openrouter_admin.httpx.post")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_happy_path_returns_key_and_hash(self, mock_kv, mock_post):
        mock_kv.return_value = "fake-mgmt-key"
        mock_post.return_value = _mock_response(
            201,
            {
                "key": "mock-or-key-xyz",
                "data": {
                    "hash": "abc123",
                    "name": "tenant-148ccf1c",
                    "label": "tenant-148ccf1c",
                    "limit": 5.0,
                    "limit_reset": "monthly",
                    "usage_monthly": 0,
                },
            },
        )

        api_key, key_hash = create_sub_key("tenant-148ccf1c", limit_dollars=5.0)

        self.assertEqual(api_key, "mock-or-key-xyz")
        self.assertEqual(key_hash, "abc123")

        # Verify request shape: Bearer header + correct body.
        call = mock_post.call_args
        self.assertEqual(call.args[0], "https://openrouter.ai/api/v1/keys")
        self.assertEqual(
            call.kwargs["headers"]["Authorization"],
            "Bearer fake-mgmt-key",
        )
        self.assertEqual(call.kwargs["json"]["name"], "tenant-148ccf1c")
        self.assertEqual(call.kwargs["json"]["limit"], 5.0)
        self.assertEqual(call.kwargs["json"]["limit_reset"], "monthly")

    @patch("apps.billing.openrouter_admin.httpx.post")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_raises_on_http_error(self, mock_kv, mock_post):
        mock_kv.return_value = "fake-mgmt-key"
        mock_post.return_value = _mock_response(403, {"error": "forbidden"}, "forbidden")
        with self.assertRaises(OpenRouterAdminError) as ctx:
            create_sub_key("tenant-x", limit_dollars=5.0)
        self.assertEqual(ctx.exception.status, 403)

    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_invalid_limit_reset_rejected_locally(self, mock_kv):
        mock_kv.return_value = "fake-mgmt-key"
        with self.assertRaises(OpenRouterAdminError):
            create_sub_key("tenant-x", limit_dollars=5.0, limit_reset="quarterly")

    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_management_key_missing_raises(self, mock_kv):
        mock_kv.return_value = None
        with self.assertRaises(OpenRouterAdminError) as ctx:
            create_sub_key("tenant-x", limit_dollars=5.0)
        self.assertIn("management key", str(ctx.exception).lower())

    @patch("apps.billing.openrouter_admin.httpx.post")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_response_missing_key_or_hash_raises(self, mock_kv, mock_post):
        mock_kv.return_value = "fake-mgmt-key"
        # OR returned 201 but didn't include the key string somehow.
        mock_post.return_value = _mock_response(201, {"data": {"hash": "abc"}})
        with self.assertRaises(OpenRouterAdminError):
            create_sub_key("tenant-x", limit_dollars=5.0)


@override_settings(
    AZURE_KV_SECRET_OPENROUTER_MANAGEMENT_KEY="openrouter-management-key",
    OPENROUTER_API_BASE="https://openrouter.ai/api/v1",
)
class DeleteSubKeyTest(TestCase):
    @patch("apps.billing.openrouter_admin.httpx.delete")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_happy_path(self, mock_kv, mock_delete):
        mock_kv.return_value = "fake-mgmt-key"
        mock_delete.return_value = _mock_response(200, {"deleted": True})
        delete_sub_key("abc123")  # no return; raises on failure
        mock_delete.assert_called_once()
        url = mock_delete.call_args.args[0]
        self.assertEqual(url, "https://openrouter.ai/api/v1/keys/abc123")

    @patch("apps.billing.openrouter_admin.httpx.delete")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_404_treated_as_success(self, mock_kv, mock_delete):
        mock_kv.return_value = "fake-mgmt-key"
        mock_delete.return_value = _mock_response(404, {"error": "not found"})
        delete_sub_key("abc123")  # should NOT raise

    @patch("apps.billing.openrouter_admin.httpx.delete")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_other_4xx_raises(self, mock_kv, mock_delete):
        mock_kv.return_value = "fake-mgmt-key"
        mock_delete.return_value = _mock_response(403, {"error": "forbidden"}, "forbidden")
        with self.assertRaises(OpenRouterAdminError):
            delete_sub_key("abc123")

    @patch("apps.billing.openrouter_admin.httpx.delete")
    @patch("apps.orchestrator.azure_client.read_key_vault_secret")
    def test_empty_hash_is_noop(self, mock_kv, mock_delete):
        mock_kv.return_value = "fake-mgmt-key"
        delete_sub_key("")
        mock_delete.assert_not_called()


@override_settings(OPENROUTER_API_BASE="https://openrouter.ai/api/v1")
class GetKeyUsageTest(TestCase):
    @patch("apps.billing.openrouter_admin.httpx.get")
    def test_returns_decimal_from_usage_monthly(self, mock_get):
        mock_get.return_value = _mock_response(
            200,
            {
                "data": {
                    "label": "tenant-148ccf1c",
                    "usage": 1.23,
                    "usage_monthly": 1.23,
                }
            },
        )
        self.assertEqual(get_key_usage("mock-or-key-xyz"), Decimal("1.23"))
        # Verify Bearer header uses the supplied API key, not management.
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["Authorization"],
            "Bearer mock-or-key-xyz",
        )

    @patch("apps.billing.openrouter_admin.httpx.get")
    def test_returns_zero_on_http_error(self, mock_get):
        mock_get.return_value = _mock_response(500, text_body="boom")
        # Reconcile cron tolerates per-tenant failures; should not raise.
        self.assertEqual(get_key_usage("mock-or-key-xyz"), Decimal("0"))

    @patch("apps.billing.openrouter_admin.httpx.get")
    def test_returns_zero_when_field_missing(self, mock_get):
        mock_get.return_value = _mock_response(200, {"data": {"usage": 5.0}})
        self.assertEqual(get_key_usage("mock-or-key-xyz"), Decimal("0"))

    @patch("apps.billing.openrouter_admin.httpx.get")
    def test_empty_api_key_returns_zero_without_calling(self, mock_get):
        self.assertEqual(get_key_usage(""), Decimal("0"))
        mock_get.assert_not_called()


class SecretNameForTenantTest(TestCase):
    def test_uses_key_vault_prefix(self):
        tenant = MagicMock()
        tenant.key_vault_prefix = "tenants-148ccf1c"
        self.assertEqual(
            secret_name_for_tenant(tenant),
            "tenants-148ccf1c-openrouter-key",
        )

    def test_missing_prefix_raises(self):
        tenant = MagicMock()
        tenant.key_vault_prefix = ""
        with self.assertRaises(OpenRouterAdminError):
            secret_name_for_tenant(tenant)
