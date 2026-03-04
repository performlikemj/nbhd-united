"""Tests for the LINE integration — webhook view, linking flow, channel-aware delivery."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.tenants.models import Tenant, User
from apps.tenants.line_models import LineLinkToken


def _make_signature(body: bytes, secret: str = "test-secret") -> str:
    """Compute X-Line-Signature for a body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _make_user(**kwargs) -> User:
    defaults = {
        "username": f"u_{secrets.token_hex(4)}",
        "email": f"{secrets.token_hex(4)}@example.com",
    }
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def _make_tenant(user: User, **kwargs) -> Tenant:
    defaults = {
        "status": Tenant.Status.ACTIVE,
        "container_fqdn": "container.example.com",
    }
    defaults.update(kwargs)
    return Tenant.objects.create(user=user, **defaults)


@override_settings(LINE_CHANNEL_SECRET="test-secret", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class LineWebhookViewTest(TestCase):
    """Test suite for LineWebhookView."""

    def setUp(self):
        from apps.router.line_webhook import LineWebhookView
        self.factory = RequestFactory()
        self.view = LineWebhookView.as_view()

    def _post(self, body: dict, secret: str = "test-secret") -> object:
        raw = json.dumps(body).encode()
        sig = _make_signature(raw, secret)
        request = self.factory.post(
            "/api/v1/line/webhook/",
            data=raw,
            content_type="application/json",
            HTTP_X_LINE_SIGNATURE=sig,
        )
        return self.view(request)

    def test_missing_signature_returns_403(self):
        raw = json.dumps({"events": []}).encode()
        request = self.factory.post(
            "/api/v1/line/webhook/",
            data=raw,
            content_type="application/json",
        )
        from apps.router.line_webhook import LineWebhookView
        response = LineWebhookView.as_view()(request)
        self.assertEqual(response.status_code, 403)

    def test_wrong_signature_returns_403(self):
        raw = json.dumps({"events": []}).encode()
        request = self.factory.post(
            "/api/v1/line/webhook/",
            data=raw,
            content_type="application/json",
            HTTP_X_LINE_SIGNATURE="badsig",
        )
        from apps.router.line_webhook import LineWebhookView
        response = LineWebhookView.as_view()(request)
        self.assertEqual(response.status_code, 403)

    def test_valid_empty_payload_returns_200(self):
        response = self._post({"events": []})
        self.assertEqual(response.status_code, 200)

    def test_unfollow_clears_line_user_id(self):
        user = _make_user(line_user_id="U_unfollow_test")
        body = {
            "events": [
                {
                    "type": "unfollow",
                    "source": {"userId": "U_unfollow_test"},
                }
            ]
        }
        response = self._post(body)
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertIsNone(user.line_user_id)

    @patch("apps.router.line_webhook._send_line_push")
    def test_follow_event_sends_welcome(self, mock_push):
        body = {
            "events": [
                {
                    "type": "follow",
                    "source": {"userId": "U_follow_test"},
                }
            ]
        }
        response = self._post(body)
        self.assertEqual(response.status_code, 200)
        # The actual push happens in a thread; give it a moment
        import time
        time.sleep(0.1)
        mock_push.assert_called_once()
        args = mock_push.call_args[0]
        self.assertEqual(args[0], "U_follow_test")

    @patch("apps.router.line_webhook._forward_to_container")
    @patch("apps.router.line_webhook._send_line_push")
    def test_message_event_dispatches_async(self, mock_push, mock_fwd):
        user = _make_user(line_user_id="U_msg_test")
        _make_tenant(user)
        body = {
            "events": [
                {
                    "type": "message",
                    "source": {"userId": "U_msg_test"},
                    "message": {"type": "text", "text": "Hello"},
                }
            ]
        }
        response = self._post(body)
        self.assertEqual(response.status_code, 200)
        # Thread may not have run yet — that's fine, just check 200 returned immediately


@override_settings(LINE_CHANNEL_SECRET="test-secret", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class LineLinkingFlowTest(TestCase):
    """Test suite for the LINE account linking flow."""

    def test_process_link_token_valid(self):
        from apps.router.line_service import process_line_link_token

        user = _make_user()
        token_value = secrets.token_urlsafe(32)
        LineLinkToken.objects.create(
            user=user,
            token=token_value,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        success, msg = process_line_link_token(
            line_user_id="U_new_user",
            line_display_name="Test User",
            token=token_value,
        )

        self.assertTrue(success)
        user.refresh_from_db()
        self.assertEqual(user.line_user_id, "U_new_user")
        self.assertEqual(user.line_display_name, "Test User")

    def test_process_link_token_expired(self):
        from apps.router.line_service import process_line_link_token

        user = _make_user()
        token_value = secrets.token_urlsafe(32)
        LineLinkToken.objects.create(
            user=user,
            token=token_value,
            expires_at=timezone.now() - timedelta(minutes=1),  # already expired
        )

        success, msg = process_line_link_token(
            line_user_id="U_some_user",
            line_display_name="",
            token=token_value,
        )

        self.assertFalse(success)
        self.assertIn("expired", msg.lower())

    def test_process_link_token_invalid(self):
        from apps.router.line_service import process_line_link_token

        success, msg = process_line_link_token(
            line_user_id="U_some_user",
            line_display_name="",
            token="nonexistent-token",
        )

        self.assertFalse(success)
        self.assertIn("invalid", msg.lower())

    def test_process_link_token_already_linked_to_another(self):
        from apps.router.line_service import process_line_link_token

        # user1 owns the token
        user1 = _make_user()
        # user2 already has this LINE ID linked
        _make_user(line_user_id="U_taken")

        token_value = secrets.token_urlsafe(32)
        LineLinkToken.objects.create(
            user=user1,
            token=token_value,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        success, msg = process_line_link_token(
            line_user_id="U_taken",
            line_display_name="",
            token=token_value,
        )

        self.assertFalse(success)
        self.assertIn("already linked", msg.lower())

    def test_unlink_line(self):
        from apps.router.line_service import unlink_line

        user = _make_user(line_user_id="U_to_unlink", line_display_name="Linked User")
        result = unlink_line(user)
        self.assertTrue(result)
        user.refresh_from_db()
        self.assertIsNone(user.line_user_id)
        self.assertEqual(user.line_display_name, "")

    def test_unlink_line_not_linked(self):
        from apps.router.line_service import unlink_line

        user = _make_user()
        result = unlink_line(user)
        self.assertFalse(result)

    def test_generate_link_token(self):
        from apps.router.line_service import generate_link_token, get_deep_link

        user = _make_user()
        token = generate_link_token(user)
        self.assertTrue(token.is_valid)
        self.assertFalse(token.used)

        deep_link = get_deep_link(token.token)
        self.assertIn("line.me", deep_link)
        self.assertIn("link_", deep_link)


@override_settings(
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
    TELEGRAM_BOT_TOKEN="test-tg-token",
)
class CronDeliveryChannelRoutingTest(TestCase):
    """Test channel-aware routing in CronDeliveryView."""

    def setUp(self):
        self.factory = RequestFactory()
        from apps.router.cron_delivery import CronDeliveryView
        self.view = CronDeliveryView.as_view()

    def _make_auth_headers(self, tenant: Tenant) -> dict:
        from apps.integrations.internal_auth import NBHD_INTERNAL_API_KEY as _  # noqa
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(tenant.id),
        }

    @override_settings(
        NBHD_INTERNAL_API_KEY="test-internal-key",
        NBHD_INTERNAL_API_KEY_FALLBACK_ENABLED=True,
    )
    @patch("httpx.Client")
    def test_routes_to_telegram_by_default(self, mock_client_cls):
        """User with telegram linked and default preferred_channel → Telegram."""
        user = _make_user(
            telegram_chat_id=123456,
            preferred_channel="telegram",
        )
        tenant = _make_tenant(user)

        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=MagicMock(
            post=MagicMock(return_value=mock_resp)
        ))
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        request = self.factory.post(
            f"/api/v1/integrations/runtime/{tenant.id}/send-to-user/",
            data=json.dumps({"message": "Hello from cron"}),
            content_type="application/json",
            **self._make_auth_headers(tenant),
        )
        response = self.view(request, tenant_id=str(tenant.id))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data.get("channel"), "telegram")

    @override_settings(
        NBHD_INTERNAL_API_KEY="test-internal-key",
        NBHD_INTERNAL_API_KEY_FALLBACK_ENABLED=True,
    )
    @patch("httpx.Client")
    def test_routes_to_line_when_preferred(self, mock_client_cls):
        """User with LINE linked and preferred_channel=line → LINE."""
        user = _make_user(
            line_user_id="U_cron_test",
            preferred_channel="line",
        )
        tenant = _make_tenant(user)

        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=MagicMock(
            post=MagicMock(return_value=mock_resp)
        ))
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        request = self.factory.post(
            f"/api/v1/integrations/runtime/{tenant.id}/send-to-user/",
            data=json.dumps({"message": "Hello via LINE"}),
            content_type="application/json",
            **self._make_auth_headers(tenant),
        )
        response = self.view(request, tenant_id=str(tenant.id))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data.get("channel"), "line")

    @override_settings(
        NBHD_INTERNAL_API_KEY="test-internal-key",
        NBHD_INTERNAL_API_KEY_FALLBACK_ENABLED=True,
    )
    def test_returns_422_when_no_channel_linked(self):
        """User with no Telegram or LINE linked → 422."""
        user = _make_user()
        tenant = _make_tenant(user)

        request = self.factory.post(
            f"/api/v1/integrations/runtime/{tenant.id}/send-to-user/",
            data=json.dumps({"message": "Hello"}),
            content_type="application/json",
            **self._make_auth_headers(tenant),
        )
        response = self.view(request, tenant_id=str(tenant.id))
        self.assertEqual(response.status_code, 422)
        data = json.loads(response.content)
        self.assertIn("no_channel_linked", data.get("error", ""))

    def test_resolve_channel_prefers_line(self):
        """_resolve_channel returns 'line' when preferred and linked."""
        from apps.router.cron_delivery import CronDeliveryView
        view = CronDeliveryView()

        user = MagicMock()
        user.preferred_channel = "line"
        user.line_user_id = "U_123"
        user.telegram_chat_id = 456  # both linked

        self.assertEqual(view._resolve_channel(user), "line")

    def test_resolve_channel_falls_back_to_linked(self):
        """_resolve_channel falls back to whichever channel is actually linked."""
        from apps.router.cron_delivery import CronDeliveryView
        view = CronDeliveryView()

        user = MagicMock()
        user.preferred_channel = "telegram"
        user.line_user_id = "U_123"
        user.telegram_chat_id = None  # Telegram not linked

        self.assertEqual(view._resolve_channel(user), "line")

    def test_resolve_channel_none_when_unlinked(self):
        from apps.router.cron_delivery import CronDeliveryView
        view = CronDeliveryView()

        user = MagicMock()
        user.preferred_channel = "telegram"
        user.line_user_id = None
        user.telegram_chat_id = None

        self.assertIsNone(view._resolve_channel(user))


class LineServicesTest(TestCase):
    """Unit tests for line_service helpers."""

    def test_resolve_tenant_by_line_user_id_active(self):
        from apps.router.line_service import resolve_tenant_by_line_user_id

        user = _make_user(line_user_id="U_resolve_test")
        tenant = _make_tenant(user)

        result = resolve_tenant_by_line_user_id("U_resolve_test")
        self.assertIsNotNone(result)
        self.assertEqual(result.id, tenant.id)

    def test_resolve_tenant_by_line_user_id_unknown(self):
        from apps.router.line_service import resolve_tenant_by_line_user_id

        result = resolve_tenant_by_line_user_id("U_nonexistent")
        self.assertIsNone(result)

    def test_get_line_status_linked(self):
        from apps.router.line_service import get_line_status

        user = _make_user(line_user_id="U_status_test", line_display_name="Display")
        result = get_line_status(user)
        self.assertTrue(result["linked"])
        self.assertEqual(result["line_display_name"], "Display")

    def test_get_line_status_unlinked(self):
        from apps.router.line_service import get_line_status

        user = _make_user()
        result = get_line_status(user)
        self.assertFalse(result["linked"])
