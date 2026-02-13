"""Additional router service coverage."""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

import httpx
from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.router.services import (
    clear_cache,
    clear_rate_limits,
    forward_to_openclaw,
    is_rate_limited,
    resolve_container,
    send_temporary_error,
)


class ResolveContainerEdgeCaseTest(TestCase):
    def setUp(self):
        clear_cache()
        clear_rate_limits()
        self.tenant = create_tenant(display_name="Router", telegram_chat_id=111999)
        self.tenant.container_fqdn = "oc-router.internal.azurecontainerapps.io"
        self.tenant.save(update_fields=["container_fqdn", "updated_at"])

    def tearDown(self):
        clear_cache()
        clear_rate_limits()

    def test_inactive_tenant_returns_none(self):
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.save(update_fields=["status", "updated_at"])

        self.assertIsNone(resolve_container(111999))


class ForwardingBehaviorTest(TestCase):
    @patch("apps.router.services.httpx.AsyncClient")
    def test_forward_to_openclaw_success(self, mock_async_client):
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.content = b'{"ok": true}'
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True}
        mock_client.post.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client

        result = asyncio.run(
            forward_to_openclaw("oc-router.internal.azurecontainerapps.io", {"message": {"chat": {"id": 1}}})
        )

        self.assertEqual(result, {"ok": True})
        mock_client.post.assert_called_once_with(
            "http://oc-router.internal.azurecontainerapps.io/telegram-webhook",
            json={"message": {"chat": {"id": 1}}},
        )

    @patch("apps.router.services.httpx.AsyncClient")
    def test_forward_to_openclaw_timeout_exhausts_retries(self, mock_async_client):
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.TimeoutException("timeout")
        mock_async_client.return_value.__aenter__.return_value = mock_client

        result = asyncio.run(
            forward_to_openclaw(
                "oc-router.internal.azurecontainerapps.io",
                {"message": {"chat": {"id": 1}}},
                max_retries=1,
                retry_delay=0.0,
            )
        )
        self.assertIsNone(result)
        self.assertEqual(mock_client.post.call_count, 2)  # initial + 1 retry

    @patch("apps.router.services.httpx.AsyncClient")
    def test_forward_to_openclaw_retry_succeeds(self, mock_async_client):
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.content = b'{"ok": true}'
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True}
        mock_client.post.side_effect = [
            httpx.TimeoutException("cold start"),
            mock_response,
        ]
        mock_async_client.return_value.__aenter__.return_value = mock_client

        result = asyncio.run(
            forward_to_openclaw(
                "oc-router.internal.azurecontainerapps.io",
                {"message": {"chat": {"id": 1}}},
                retry_delay=0.0,
            )
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_client.post.call_count, 2)

    @patch("apps.router.services.httpx.AsyncClient")
    def test_forward_to_openclaw_http_error_returns_none(self, mock_async_client):
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.HTTPError("bad gateway")
        mock_async_client.return_value.__aenter__.return_value = mock_client

        result = asyncio.run(
            forward_to_openclaw(
                "oc-router.internal.azurecontainerapps.io",
                {"message": {"chat": {"id": 1}}},
                max_retries=0,
            )
        )
        self.assertIsNone(result)

    @patch("apps.router.services.httpx.AsyncClient")
    def test_forward_url_uses_port_80(self, mock_async_client):
        """URL must not include :18789 — Azure internal ingress serves on port 80."""
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.content = b"{}"
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {}
        mock_client.post.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client

        asyncio.run(
            forward_to_openclaw("oc-test.internal.azurecontainerapps.io", {})
        )

        url = mock_client.post.call_args[0][0]
        self.assertNotIn(":18789", url)
        self.assertEqual(url, "http://oc-test.internal.azurecontainerapps.io/telegram-webhook")


class SendTemporaryErrorTest(TestCase):
    def test_returns_send_message_payload(self):
        result = send_temporary_error(12345)
        self.assertEqual(result["method"], "sendMessage")
        self.assertEqual(result["chat_id"], 12345)
        self.assertIn("waking up", result["text"])


@override_settings(ROUTER_RATE_LIMIT_PER_MINUTE=1)
class RateLimitTest(TestCase):
    def tearDown(self):
        clear_rate_limits()

    def test_is_rate_limited_after_threshold(self):
        self.assertFalse(is_rate_limited(10))
        self.assertTrue(is_rate_limited(10))
