"""Additional router service coverage."""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

import httpx
from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.router.services import (
    ROUTE_CACHE_TTL,
    clear_cache,
    clear_rate_limits,
    forward_to_openclaw,
    is_rate_limited,
    resolve_container,
    resolve_tenant_by_chat_id,
    resolve_user_timezone,
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

    def test_resolve_tenant_by_chat_id_returns_tenant(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-router.internal.azurecontainerapps.io"
        self.tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

        resolved = resolve_tenant_by_chat_id(111999)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, self.tenant.id)

    def test_resolve_tenant_by_chat_id_returns_suspended_tenant(self):
        """Suspended tenants are returned so the webhook can send trial-ended messages."""
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.container_fqdn = ""
        self.tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

        resolved = resolve_tenant_by_chat_id(111999)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.status, Tenant.Status.SUSPENDED)

    def test_resolve_tenant_by_chat_id_returns_tenant_for_pending(self):
        """Pending tenants are returned so poller can send 'waking up' message."""
        self.tenant.status = Tenant.Status.PENDING
        self.tenant.save(update_fields=["status", "updated_at"])

        tenant = resolve_tenant_by_chat_id(111999)
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.status, Tenant.Status.PENDING)

    def test_resolve_user_timezone_returns_utc_when_user_missing(self):
        self.assertEqual(resolve_user_timezone(404404), "UTC")

    def test_resolve_user_timezone_returns_user_preference(self):
        self.tenant.user.timezone = "America/Los_Angeles"
        self.tenant.user.save(update_fields=["timezone"])
        self.assertEqual(resolve_user_timezone(111999), "America/Los_Angeles")


class RouteCacheTTLTest(TestCase):
    def setUp(self):
        clear_cache()
        self.tenant = create_tenant(display_name="TTL", telegram_chat_id=222888)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-ttl.internal.azurecontainerapps.io"
        self.tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

    def tearDown(self):
        clear_cache()

    def test_expired_cache_entry_triggers_db_lookup(self):
        """After TTL expires, resolve_container should re-query the DB."""
        # Populate the cache
        fqdn = resolve_container(222888)
        self.assertEqual(fqdn, "oc-ttl.internal.azurecontainerapps.io")

        # Simulate TTL expiry by backdating the cached timestamp
        from apps.router.services import _route_cache
        from time import monotonic
        _route_cache[222888] = ("old-stale.internal.azurecontainerapps.io", monotonic() - ROUTE_CACHE_TTL - 1)

        # Should ignore the stale entry and get the fresh value from DB
        fqdn = resolve_container(222888)
        self.assertEqual(fqdn, "oc-ttl.internal.azurecontainerapps.io")


@override_settings(TELEGRAM_WEBHOOK_SECRET="test-webhook-secret")
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
            "https://oc-router.internal.azurecontainerapps.io/telegram-webhook",
            json={"message": {"chat": {"id": 1}}},
            headers={
                "X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret",
                "X-User-Timezone": "UTC",
            },
        )

    @patch("apps.router.services.httpx.AsyncClient")
    def test_forward_to_openclaw_passes_user_timezone_header(self, mock_async_client):
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.content = b'{"ok": true}'
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True}
        mock_client.post.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client

        asyncio.run(
            forward_to_openclaw(
                "oc-router.internal.azurecontainerapps.io",
                {"message": {"chat": {"id": 1}}},
                user_timezone="Asia/Tokyo",
            )
        )

        self.assertEqual(
            mock_client.post.call_args.kwargs["headers"]["X-User-Timezone"],
            "Asia/Tokyo",
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
                max_retries=1,
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
        self.assertEqual(url, "https://oc-test.internal.azurecontainerapps.io/telegram-webhook")

    @patch("apps.router.services.httpx.AsyncClient")
    def test_forward_default_no_retries(self, mock_async_client):
        """Default: single attempt, no retries on timeout."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.TimeoutException("cold start")
        mock_async_client.return_value.__aenter__.return_value = mock_client

        result = asyncio.run(
            forward_to_openclaw("oc-test.internal.azurecontainerapps.io", {})
        )
        self.assertIsNone(result)
        self.assertEqual(mock_client.post.call_count, 1)


class SendTemporaryErrorTest(TestCase):
    def test_returns_send_message_payload(self):
        result = send_temporary_error(12345)
        self.assertEqual(result["method"], "sendMessage")
        self.assertEqual(result["chat_id"], 12345)
        self.assertIn("30 seconds", result["text"])


@override_settings(ROUTER_RATE_LIMIT_PER_MINUTE=1)
class RateLimitTest(TestCase):
    def tearDown(self):
        clear_rate_limits()

    def test_is_rate_limited_after_threshold(self):
        self.assertFalse(is_rate_limited(10))
        self.assertTrue(is_rate_limited(10))
