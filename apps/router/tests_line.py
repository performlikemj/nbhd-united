"""Tests for the LINE integration — webhook view, linking flow, channel-aware delivery."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
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


# ────────────────────────────────────────────────────────────────────────────
# Webhook View Tests
# ────────────────────────────────────────────────────────────────────────────


@override_settings(LINE_CHANNEL_SECRET="test-secret", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class LineWebhookSignatureTest(TestCase):
    """Signature verification tests for LineWebhookView."""

    def setUp(self):
        from apps.router.line_webhook import LineWebhookView
        self.factory = RequestFactory()
        self.view = LineWebhookView.as_view()

    def _post(self, body: dict, sig: str | None = None, secret: str = "test-secret"):
        raw = json.dumps(body).encode()
        headers = {}
        if sig is not None:
            headers["HTTP_X_LINE_SIGNATURE"] = sig
        elif sig is None and secret:
            headers["HTTP_X_LINE_SIGNATURE"] = _make_signature(raw, secret)
        return self.view(
            self.factory.post(
                "/api/v1/line/webhook/",
                data=raw,
                content_type="application/json",
                **headers,
            )
        )

    def test_missing_signature_returns_403(self):
        raw = json.dumps({"events": []}).encode()
        request = self.factory.post(
            "/api/v1/line/webhook/",
            data=raw,
            content_type="application/json",
            # No X-Line-Signature header
        )
        response = self.view(request)
        self.assertEqual(response.status_code, 403)

    def test_wrong_signature_returns_403(self):
        raw = json.dumps({"events": []}).encode()
        request = self.factory.post(
            "/api/v1/line/webhook/",
            data=raw,
            content_type="application/json",
            HTTP_X_LINE_SIGNATURE="badsig",
        )
        response = self.view(request)
        self.assertEqual(response.status_code, 403)

    def test_valid_empty_payload_returns_200(self):
        response = self._post({"events": []})
        self.assertEqual(response.status_code, 200)


@override_settings(LINE_CHANNEL_SECRET="test-secret", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class LineWebhookEventTest(TestCase):
    """Test LINE webhook event handling."""

    def setUp(self):
        from apps.router.line_webhook import LineWebhookView
        self.factory = RequestFactory()
        self.view = LineWebhookView.as_view()

    def _post(self, body: dict):
        raw = json.dumps(body).encode()
        sig = _make_signature(raw)
        return self.view(
            self.factory.post(
                "/api/v1/line/webhook/",
                data=raw,
                content_type="application/json",
                HTTP_X_LINE_SIGNATURE=sig,
            )
        )

    def test_unfollow_clears_line_user_id(self):
        """Unfollow event clears the user's line_user_id (direct call, not via thread)."""
        from apps.router.line_webhook import LineWebhookView

        user = _make_user(line_user_id="U_unfollow_test")
        event = {
            "type": "unfollow",
            "source": {"userId": "U_unfollow_test"},
        }
        # Call handler directly to avoid thread timing issues in tests
        view = LineWebhookView()
        view._handle_unfollow(event)

        user.refresh_from_db()
        self.assertIsNone(user.line_user_id)

    @patch("apps.router.line_webhook._send_line_push")
    def test_follow_event_sends_welcome(self, mock_push):
        mock_push.return_value = True
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
        time.sleep(0.2)
        mock_push.assert_called()
        args = mock_push.call_args[0]
        self.assertEqual(args[0], "U_follow_test")

    @patch("apps.router.line_webhook.LineWebhookView._forward_to_container")
    @patch("apps.router.line_webhook._send_line_push")
    def test_unknown_user_message_sends_signup_link(self, mock_push, mock_fwd):
        """Unknown LINE user receives a signup prompt — called directly to avoid SQLite thread lock."""
        from apps.router.line_webhook import LineWebhookView

        mock_push.return_value = True
        event = {
            "type": "message",
            "source": {"userId": "U_unknown_direct"},
            "message": {"type": "text", "text": "Hello"},
        }
        # Call handler directly — bypasses thread and SQLite locking
        view = LineWebhookView()
        view._handle_message(event)

        # Should have sent a signup prompt, not forwarded
        mock_fwd.assert_not_called()
        mock_push.assert_called()
        call_args = mock_push.call_args[0]
        self.assertEqual(call_args[0], "U_unknown_direct")

    @patch("httpx.post")
    def test_message_from_linked_user_forwards(self, mock_httpx_post):
        """Linked user's message triggers container forward — called directly."""
        from apps.router.line_webhook import LineWebhookView

        user = _make_user(line_user_id="U_linked_direct")
        _make_tenant(user)

        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Hi there!"}}],
            "usage": {},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_httpx_post.return_value = mock_resp

        event = {
            "type": "message",
            "source": {"userId": "U_linked_direct"},
            "message": {"type": "text", "text": "Hello"},
        }
        # Call handler directly — bypasses thread and SQLite locking
        view = LineWebhookView()
        view._handle_message(event)

        # Should have posted to container and then LINE push API
        self.assertGreaterEqual(mock_httpx_post.call_count, 1)


# ────────────────────────────────────────────────────────────────────────────
# Account Linking Tests
# ────────────────────────────────────────────────────────────────────────────


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

        # Token should be marked as used
        token = LineLinkToken.objects.get(token=token_value)
        self.assertTrue(token.used)

    def test_process_link_token_expired(self):
        from apps.router.line_service import process_line_link_token

        user = _make_user()
        token_value = secrets.token_urlsafe(32)
        LineLinkToken.objects.create(
            user=user,
            token=token_value,
            expires_at=timezone.now() - timedelta(minutes=1),
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

        user1 = _make_user()
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

    def test_process_link_sets_display_name_on_friend(self):
        """If user's display_name is 'Friend', update it from LINE profile."""
        from apps.router.line_service import process_line_link_token

        user = _make_user(display_name="Friend")
        token_value = secrets.token_urlsafe(32)
        LineLinkToken.objects.create(
            user=user,
            token=token_value,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        process_line_link_token(
            line_user_id="U_display",
            line_display_name="LINE Display Name",
            token=token_value,
        )

        user.refresh_from_db()
        self.assertEqual(user.display_name, "LINE Display Name")

    def test_unlink_line(self):
        from apps.router.line_service import unlink_line

        user = _make_user(
            line_user_id="U_to_unlink",
            line_display_name="Linked User",
            preferred_channel="line",
        )
        result = unlink_line(user)
        self.assertTrue(result)
        user.refresh_from_db()
        self.assertIsNone(user.line_user_id)
        self.assertEqual(user.line_display_name, "")
        self.assertEqual(user.preferred_channel, "telegram")  # switched back

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
        self.assertIn(f"link_{token.token}", deep_link)

    def test_get_line_status_linked(self):
        from apps.router.line_service import get_line_status

        user = _make_user(line_user_id="U_status", line_display_name="Display")
        result = get_line_status(user)
        self.assertTrue(result["linked"])
        self.assertEqual(result["line_display_name"], "Display")

    def test_get_line_status_unlinked(self):
        from apps.router.line_service import get_line_status

        user = _make_user()
        result = get_line_status(user)
        self.assertFalse(result["linked"])


# ────────────────────────────────────────────────────────────────────────────
# Channel-Aware Delivery Tests
# ────────────────────────────────────────────────────────────────────────────


class CronDeliveryChannelRoutingTest(TestCase):
    """Test _resolve_channel logic in CronDeliveryView."""

    def _get_view(self):
        from apps.router.cron_delivery import CronDeliveryView
        return CronDeliveryView()

    def test_resolve_channel_prefers_telegram_when_linked(self):
        view = self._get_view()
        user = MagicMock()
        user.preferred_channel = "telegram"
        user.telegram_chat_id = 123
        user.line_user_id = "U_123"
        self.assertEqual(view._resolve_channel(user), "telegram")

    def test_resolve_channel_prefers_line_when_linked(self):
        view = self._get_view()
        user = MagicMock()
        user.preferred_channel = "line"
        user.line_user_id = "U_123"
        user.telegram_chat_id = 456
        self.assertEqual(view._resolve_channel(user), "line")

    def test_resolve_channel_falls_back_to_linked(self):
        view = self._get_view()
        user = MagicMock()
        user.preferred_channel = "telegram"
        user.line_user_id = "U_123"
        user.telegram_chat_id = None
        self.assertEqual(view._resolve_channel(user), "line")

    def test_resolve_channel_none_when_unlinked(self):
        view = self._get_view()
        user = MagicMock()
        user.preferred_channel = "telegram"
        user.line_user_id = None
        user.telegram_chat_id = None
        self.assertIsNone(view._resolve_channel(user))


# ────────────────────────────────────────────────────────────────────────────
# Service Layer Tests
# ────────────────────────────────────────────────────────────────────────────


class ResolveByLineUserIdTest(TestCase):
    """Test resolve_tenant_by_line_user_id in services.py."""

    def test_active_tenant(self):
        from apps.router.services import resolve_tenant_by_line_user_id

        user = _make_user(line_user_id="U_resolve_active")
        tenant = _make_tenant(user)

        result = resolve_tenant_by_line_user_id("U_resolve_active")
        self.assertIsNotNone(result)
        self.assertEqual(result.id, tenant.id)

    def test_nonexistent_user(self):
        from apps.router.services import resolve_tenant_by_line_user_id

        result = resolve_tenant_by_line_user_id("U_nonexistent")
        self.assertIsNone(result)

    def test_suspended_tenant(self):
        from apps.router.services import resolve_tenant_by_line_user_id

        user = _make_user(line_user_id="U_suspended")
        _make_tenant(user, status=Tenant.Status.SUSPENDED)

        result = resolve_tenant_by_line_user_id("U_suspended")
        self.assertIsNotNone(result)


# ────────────────────────────────────────────────────────────────────────────
# Webhook Utility Tests
# ────────────────────────────────────────────────────────────────────────────


class LineWebhookUtilsTest(TestCase):
    """Test utility functions in line_webhook.py."""

    def test_verify_signature_valid(self):
        from apps.router.line_webhook import _verify_signature

        body = b'{"events":[]}'
        secret = "test-secret"
        sig = _make_signature(body, secret)

        with self.settings(LINE_CHANNEL_SECRET=secret):
            self.assertTrue(_verify_signature(body, sig))

    def test_verify_signature_invalid(self):
        from apps.router.line_webhook import _verify_signature

        with self.settings(LINE_CHANNEL_SECRET="test-secret"):
            self.assertFalse(_verify_signature(b"body", "badsig"))

    def test_strip_markdown(self):
        from apps.router.line_webhook import _strip_markdown

        self.assertEqual(_strip_markdown("**bold**"), "bold")
        self.assertEqual(_strip_markdown("*italic*"), "italic")
        self.assertEqual(_strip_markdown("[link](http://example.com)"), "link: http://example.com")
        self.assertEqual(_strip_markdown("`code`"), "code")

    def test_split_message(self):
        from apps.router.line_webhook import _split_message

        # Short message
        self.assertEqual(_split_message("short"), ["short"])

        # Long message
        long_text = "a" * 6000
        chunks = _split_message(long_text, max_len=5000)
        self.assertTrue(len(chunks) > 1)
        self.assertTrue(all(len(c) <= 5000 for c in chunks))
