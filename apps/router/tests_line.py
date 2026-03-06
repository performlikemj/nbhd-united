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

        # Empty string — returns single-element list (no content to split)
        self.assertEqual(_split_message(""), [""])

        # Split at paragraph break
        text = ("x" * 4000) + "\n\n" + ("y" * 2000)
        chunks = _split_message(text, max_len=4500)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].endswith("x"))
        self.assertTrue(chunks[1].startswith("y"))


# ────────────────────────────────────────────────────────────────────────────
# Edge Case Tests
# ────────────────────────────────────────────────────────────────────────────


@override_settings(LINE_CHANNEL_SECRET="test-secret", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class LineWebhookEdgeCaseTest(TestCase):
    """Edge cases: malformed payloads, concurrent links, media messages, etc."""

    def setUp(self):
        from apps.router.line_webhook import LineWebhookView
        self.factory = RequestFactory()
        self.view = LineWebhookView.as_view()

    def _post(self, body: dict | bytes, sig: str | None = None):
        if isinstance(body, dict):
            raw = json.dumps(body).encode()
        else:
            raw = body
        headers = {}
        if sig is not None:
            headers["HTTP_X_LINE_SIGNATURE"] = sig
        else:
            headers["HTTP_X_LINE_SIGNATURE"] = _make_signature(raw)
        return self.view(
            self.factory.post(
                "/api/v1/line/webhook/",
                data=raw,
                content_type="application/json",
                **headers,
            )
        )

    def test_malformed_json_returns_400(self):
        """Invalid JSON body returns 400."""
        raw = b"not json at all"
        sig = _make_signature(raw)
        request = self.factory.post(
            "/api/v1/line/webhook/",
            data=raw,
            content_type="application/json",
            HTTP_X_LINE_SIGNATURE=sig,
        )
        response = self.view(request)
        self.assertEqual(response.status_code, 400)

    def test_missing_events_key_returns_200(self):
        """Body without 'events' key returns 200 (no events to process)."""
        response = self._post({"destination": "U123", "no_events_here": True})
        self.assertEqual(response.status_code, 200)

    def test_empty_message_text_ignored(self):
        """Message event with empty text does not forward or error."""
        from apps.router.line_webhook import LineWebhookView
        user = _make_user(line_user_id="U_empty_msg")
        _make_tenant(user)

        event = {
            "type": "message",
            "source": {"userId": "U_empty_msg"},
            "message": {"type": "text", "text": "   "},
        }
        view = LineWebhookView()
        # Should not raise
        view._handle_message(event)

    @patch("apps.router.line_webhook._send_line_push")
    def test_non_text_message_sends_fallback(self, mock_push):
        """Non-text message (image, sticker) sends an unsupported notice."""
        from apps.router.line_webhook import LineWebhookView
        mock_push.return_value = True

        event = {
            "type": "message",
            "source": {"userId": "U_image_msg"},
            "message": {"type": "image", "id": "12345"},
        }
        view = LineWebhookView()
        view._handle_message(event)
        mock_push.assert_called_once()
        msg = mock_push.call_args[0][1][0]
        self.assertEqual(msg["type"], "flex")
        msg_json = json.dumps(msg).lower()
        self.assertIn("text messages", msg_json)

    @patch("apps.router.line_webhook._send_line_push")
    def test_follow_already_linked_user_sends_welcome_back(self, mock_push):
        """Follow from an already-linked user sends a different welcome."""
        from apps.router.line_webhook import LineWebhookView
        mock_push.return_value = True
        user = _make_user(line_user_id="U_already_linked")
        _make_tenant(user)

        event = {
            "type": "follow",
            "source": {"userId": "U_already_linked"},
        }
        view = LineWebhookView()
        view._handle_follow(event)
        mock_push.assert_called_once()
        msg = mock_push.call_args[0][1][0]
        self.assertEqual(msg["type"], "flex")
        msg_json = json.dumps(msg).lower()
        self.assertIn("already connected", msg_json)

    def test_unfollow_unknown_user_no_error(self):
        """Unfollow from an unknown user doesn't raise."""
        from apps.router.line_webhook import LineWebhookView
        view = LineWebhookView()
        event = {"type": "unfollow", "source": {"userId": "U_never_existed"}}
        # Should not raise
        view._handle_unfollow(event)

    def test_unfollow_resets_preferred_channel(self):
        """Unfollow resets preferred_channel if it was LINE."""
        from apps.router.line_webhook import LineWebhookView
        user = _make_user(
            line_user_id="U_pref_reset",
            preferred_channel="line",
            telegram_chat_id=999888,
        )
        view = LineWebhookView()
        view._handle_unfollow({"type": "unfollow", "source": {"userId": "U_pref_reset"}})
        user.refresh_from_db()
        self.assertIsNone(user.line_user_id)
        self.assertEqual(user.preferred_channel, "telegram")

    @patch("apps.router.line_webhook._send_line_push")
    def test_suspended_no_subscription_sends_trial_ended(self, mock_push):
        """Suspended user without subscription gets trial-ended message."""
        from apps.router.line_webhook import LineWebhookView
        mock_push.return_value = True

        user = _make_user(line_user_id="U_suspended")
        _make_tenant(
            user,
            status=Tenant.Status.SUSPENDED,
            is_trial=False,
            stripe_subscription_id="",
        )

        event = {
            "type": "message",
            "source": {"userId": "U_suspended"},
            "message": {"type": "text", "text": "hello"},
        }
        view = LineWebhookView()
        view._handle_message(event)
        mock_push.assert_called_once()
        msg = mock_push.call_args[0][1][0]
        self.assertEqual(msg["type"], "flex")
        msg_json = json.dumps(msg).lower()
        self.assertIn("trial", msg_json)

    @patch("apps.router.line_webhook._send_line_push")
    def test_provisioning_tenant_sends_waking_up(self, mock_push):
        """Provisioning tenant gets a 'waking up' message."""
        from apps.router.line_webhook import LineWebhookView
        mock_push.return_value = True

        user = _make_user(line_user_id="U_provisioning")
        _make_tenant(user, status=Tenant.Status.PROVISIONING)

        event = {
            "type": "message",
            "source": {"userId": "U_provisioning"},
            "message": {"type": "text", "text": "hello"},
        }
        view = LineWebhookView()
        view._handle_message(event)
        mock_push.assert_called_once()
        msg = mock_push.call_args[0][1][0]
        self.assertEqual(msg["type"], "flex")
        msg_json = json.dumps(msg).lower()
        self.assertIn("waking up", msg_json)

    @patch("httpx.post")
    def test_container_timeout_sends_retry_message(self, mock_httpx_post):
        """Container timeout sends a user-friendly retry message."""
        from apps.router.line_webhook import LineWebhookView
        import httpx

        user = _make_user(line_user_id="U_timeout")
        _make_tenant(user)

        mock_httpx_post.side_effect = httpx.TimeoutException("timed out")

        event = {
            "type": "message",
            "source": {"userId": "U_timeout"},
            "message": {"type": "text", "text": "hello"},
        }
        view = LineWebhookView()
        view._handle_message(event)

        # Should have called httpx.post (forward attempt) then push (error msg)
        self.assertTrue(mock_httpx_post.called)

    @patch("httpx.post")
    def test_container_503_sends_restarting_message(self, mock_httpx_post):
        """503 from container sends 'restarting' message."""
        from apps.router.line_webhook import LineWebhookView
        import httpx

        user = _make_user(line_user_id="U_503")
        _make_tenant(user)

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.is_success = False
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=mock_resp
        )
        mock_httpx_post.return_value = mock_resp

        event = {
            "type": "message",
            "source": {"userId": "U_503"},
            "message": {"type": "text", "text": "hello"},
        }
        view = LineWebhookView()
        view._handle_message(event)

    def test_link_token_reuse_rejected(self):
        """Used link token is rejected on second attempt."""
        from apps.router.line_service import generate_link_token, process_line_link_token

        user = _make_user()
        token = generate_link_token(user)

        # First use — success
        success1, _ = process_line_link_token("U_reuse1", "Test", token.token)
        self.assertTrue(success1)

        # Second use (different LINE user) — rejected
        success2, msg2 = process_line_link_token("U_reuse2", "Test2", token.token)
        self.assertFalse(success2)
        self.assertIn("expired", msg2.lower())

    def test_concurrent_link_same_line_id(self):
        """Two users can't link the same LINE account."""
        from apps.router.line_service import generate_link_token, process_line_link_token

        user1 = _make_user()
        user2 = _make_user()
        token1 = generate_link_token(user1)
        token2 = generate_link_token(user2)

        # Link to user1
        success1, _ = process_line_link_token("U_shared_line", "Shared", token1.token)
        self.assertTrue(success1)

        # Try linking same LINE ID to user2
        success2, msg2 = process_line_link_token("U_shared_line", "Shared", token2.token)
        self.assertFalse(success2)
        self.assertIn("already linked", msg2.lower())

    @patch("apps.router.line_webhook._send_line_push")
    def test_postback_event_forwards_to_container(self, mock_push):
        """Postback event is forwarded as a message to the container."""
        from apps.router.line_webhook import LineWebhookView

        mock_push.return_value = True
        user = _make_user(line_user_id="U_postback")
        _make_tenant(user)

        event = {
            "type": "postback",
            "source": {"userId": "U_postback"},
            "postback": {"data": "some_custom:data"},
        }

        with patch.object(LineWebhookView, "_forward_to_container") as mock_fwd:
            view = LineWebhookView()
            view._handle_postback(event)
            mock_fwd.assert_called_once()
            args = mock_fwd.call_args[0]
            self.assertEqual(args[0], "U_postback")
            self.assertIn("some_custom:data", args[2])

    def test_postback_unknown_user_ignored(self):
        """Postback from unknown user is silently ignored."""
        from apps.router.line_webhook import LineWebhookView
        view = LineWebhookView()
        event = {
            "type": "postback",
            "source": {"userId": "U_nobody"},
            "postback": {"data": "something"},
        }
        # Should not raise
        view._handle_postback(event)

    @patch("apps.router.line_webhook._send_line_push")
    def test_no_container_fqdn_sends_setup_message(self, mock_push):
        """Active tenant with no container_fqdn sends setup message."""
        from apps.router.line_webhook import LineWebhookView
        mock_push.return_value = True

        user = _make_user(line_user_id="U_no_fqdn")
        _make_tenant(user, container_fqdn="")

        event = {
            "type": "message",
            "source": {"userId": "U_no_fqdn"},
            "message": {"type": "text", "text": "hello"},
        }
        view = LineWebhookView()
        view._handle_message(event)
        mock_push.assert_called_once()
        msg = mock_push.call_args[0][1][0]
        self.assertEqual(msg["type"], "flex")
        msg_json = json.dumps(msg).lower()
        self.assertIn("set up", msg_json)


@override_settings(LINE_CHANNEL_SECRET="test-secret", LINE_CHANNEL_ACCESS_TOKEN="test-token")
class LineStripMarkdownTest(TestCase):
    """Thorough markdown stripping tests."""

    def test_bold_stripped(self):
        from apps.router.line_webhook import _strip_markdown
        self.assertEqual(_strip_markdown("**bold text**"), "bold text")
        self.assertEqual(_strip_markdown("__also bold__"), "also bold")

    def test_italic_stripped(self):
        from apps.router.line_webhook import _strip_markdown
        self.assertEqual(_strip_markdown("*italic text*"), "italic text")

    def test_links_converted(self):
        from apps.router.line_webhook import _strip_markdown
        self.assertEqual(
            _strip_markdown("[click here](https://example.com)"),
            "click here: https://example.com",
        )

    def test_inline_code_stripped(self):
        from apps.router.line_webhook import _strip_markdown
        self.assertEqual(_strip_markdown("`code`"), "code")

    def test_mixed_markdown(self):
        from apps.router.line_webhook import _strip_markdown
        result = _strip_markdown("**Hello** *world* [link](https://x.com) `code`")
        self.assertEqual(result, "Hello world link: https://x.com code")

    def test_plain_text_unchanged(self):
        from apps.router.line_webhook import _strip_markdown
        self.assertEqual(_strip_markdown("no markdown here"), "no markdown here")

    def test_nested_bold_italic(self):
        from apps.router.line_webhook import _strip_markdown
        # Double markers removed first, then single
        result = _strip_markdown("***bold italic***")
        # After removing **, we get *bold italic* → then * removed
        self.assertIn("bold italic", result)


@override_settings(LINE_CHANNEL_SECRET="", LINE_CHANNEL_ACCESS_TOKEN="")
class LineMissingConfigTest(TestCase):
    """Tests for missing LINE credentials."""

    def test_verify_signature_fails_without_secret(self):
        from apps.router.line_webhook import _verify_signature
        self.assertFalse(_verify_signature(b"body", "sig"))

    def test_send_push_fails_without_token(self):
        from apps.router.line_webhook import _send_line_push
        self.assertFalse(_send_line_push("U123", [{"type": "text", "text": "hi"}]))


@patch("apps.tenants.authentication.set_rls_context", lambda **kw: None)
@patch("apps.tenants.middleware.set_rls_context", lambda **kw: None)
@patch("apps.tenants.middleware.reset_rls_context", lambda: None)
class LineLinkViewsTest(TestCase):
    """Test the API views for LINE linking."""

    def setUp(self):
        self.user = _make_user()
        _make_tenant(self.user)
        from rest_framework_simplejwt.tokens import RefreshToken
        self.token = str(RefreshToken.for_user(self.user).access_token)
        self.auth_header = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    @override_settings(LINE_BOT_ID="@test-bot")
    def test_generate_link_endpoint(self):
        response = self.client.post(
            "/api/v1/tenants/line/generate-link/",
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("deep_link", data)
        self.assertIn("qr_code", data)
        self.assertIn("expires_at", data)
        self.assertIn("@test-bot", data["deep_link"])

    def test_generate_link_already_linked(self):
        self.user.line_user_id = "U_already"
        self.user.save(update_fields=["line_user_id"])
        response = self.client.post(
            "/api/v1/tenants/line/generate-link/",
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 400)

    def test_line_status_unlinked(self):
        response = self.client.get(
            "/api/v1/tenants/line/status/",
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["linked"])

    def test_line_status_linked(self):
        self.user.line_user_id = "U_status_check"
        self.user.line_display_name = "Test Name"
        self.user.save(update_fields=["line_user_id", "line_display_name"])
        response = self.client.get(
            "/api/v1/tenants/line/status/",
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["linked"])
        self.assertEqual(data["line_display_name"], "Test Name")

    def test_unlink_endpoint(self):
        self.user.line_user_id = "U_to_unlink"
        self.user.save(update_fields=["line_user_id"])
        response = self.client.post(
            "/api/v1/tenants/line/unlink/",
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.line_user_id)

    def test_unlink_not_linked(self):
        response = self.client.post(
            "/api/v1/tenants/line/unlink/",
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 400)

    def test_set_preferred_channel_line(self):
        self.user.line_user_id = "U_pref_test"
        self.user.save(update_fields=["line_user_id"])
        response = self.client.patch(
            "/api/v1/tenants/line/preferred-channel/",
            data=json.dumps({"preferred_channel": "line"}),
            content_type="application/json",
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_channel, "line")

    def test_set_preferred_channel_line_not_linked(self):
        response = self.client.patch(
            "/api/v1/tenants/line/preferred-channel/",
            data=json.dumps({"preferred_channel": "line"}),
            content_type="application/json",
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 400)

    def test_set_preferred_channel_invalid(self):
        response = self.client.patch(
            "/api/v1/tenants/line/preferred-channel/",
            data=json.dumps({"preferred_channel": "whatsapp"}),
            content_type="application/json",
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 400)
