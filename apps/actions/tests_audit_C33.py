"""Regression tests for FA-0006 (cluster C33).

``send_gate_confirmation`` previously read ``tenant.user.preferred_channel``
directly and indexed ``_SENDERS`` (which maps only ``telegram``/``line``).
``preferred_channel`` defaults to ``"telegram"`` even for iOS-only App Store
users (DeviceToken but no telegram_chat_id/line_user_id), so the Telegram sender
ran, failed deep with a misleading "no Telegram chat_id" log, and the gate
action silently expired with no prompt ever delivered.

The fix routes channel resolution through ``resolve_user_channel`` (the same
logic the cron/proactive senders use) and, when no Telegram/LINE surface exists,
logs a clear no-surface warning instead of attempting a doomed Telegram send.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.actions import messaging
from apps.actions.messaging import send_gate_confirmation
from apps.actions.models import ActionType, PendingAction
from apps.router.models import DeviceToken
from apps.tenants.models import Tenant

User = get_user_model()


class SendGateConfirmationChannelResolutionTests(TestCase):
    def _make(self, username, **user_kwargs):
        user = User.objects.create_user(username=username, email=f"{username}@example.com", password="x", **user_kwargs)
        tenant = Tenant.objects.create(
            user=user,
            status="active",
            container_fqdn=f"{username}.example.com",
            container_id=f"oc-{username}",
        )
        action = PendingAction.objects.create(
            tenant=tenant,
            action_type=ActionType.GMAIL_DELETE,
            action_payload={"message_id": "abc"},
            display_summary="Delete email",
        )
        return tenant, action

    def _patched_senders(self, tg=None, line=None):
        """Patch the ``_SENDERS`` dispatch dict (the dict captures the original
        function references at import time, so patching the module-level names
        alone does not reach the dispatcher)."""
        tg = tg or mock.Mock(return_value=None)
        line = line or mock.Mock(return_value=None)
        editor = mock.Mock()
        return (
            mock.patch.dict(
                messaging._SENDERS,
                {"telegram": (tg, editor), "line": (line, editor)},
            ),
            tg,
            line,
        )

    def test_ios_only_user_does_not_invoke_telegram_sender(self):
        """iOS-only user (DeviceToken, no telegram/line link, default
        preferred_channel='telegram') must NOT reach the Telegram sender and
        must leave the action un-delivered (no platform message id/channel)."""
        tenant, action = self._make("c33_ios")
        # preferred_channel defaults to 'telegram'; no telegram_chat_id set.
        self.assertEqual(tenant.user.preferred_channel, "telegram")
        self.assertIsNone(tenant.user.telegram_chat_id)
        DeviceToken.objects.create(tenant=tenant, user=tenant.user, token="a" * 64, environment="production")

        patcher, tg, line = self._patched_senders()
        with patcher:
            send_gate_confirmation(tenant, action)

        tg.assert_not_called()
        line.assert_not_called()

        action.refresh_from_db()
        self.assertEqual(action.platform_message_id, "")
        self.assertEqual(action.platform_channel, "")

    def test_no_surface_user_does_not_invoke_any_sender(self):
        """User with neither messaging channel nor DeviceToken: no sender runs."""
        tenant, action = self._make("c33_none")

        patcher, tg, line = self._patched_senders()
        with patcher:
            send_gate_confirmation(tenant, action)

        tg.assert_not_called()
        line.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "")

    def test_telegram_user_still_routed_to_telegram(self):
        """Linked Telegram user is unaffected: sender runs and result stored."""
        tenant, action = self._make("c33_tg", telegram_chat_id=123456789)

        patcher, tg, line = self._patched_senders(tg=mock.Mock(return_value="555"))
        with patcher:
            send_gate_confirmation(tenant, action)

        tg.assert_called_once_with(tenant, action)
        line.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.platform_message_id, "555")
        self.assertEqual(action.platform_channel, "telegram")

    def test_line_user_routed_to_line(self):
        """Linked LINE user (preferred_channel='line') routes to the LINE sender."""
        tenant, action = self._make("c33_line", line_user_id="U" + "0" * 32, preferred_channel="line")

        patcher, tg, line = self._patched_senders(line=mock.Mock(return_value="line-push-x"))
        with patcher:
            send_gate_confirmation(tenant, action)

        line.assert_called_once_with(tenant, action)
        tg.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "line")

    def test_app_token_with_telegram_still_routes_to_telegram(self):
        """Load-bearing gate exception (MJ's case): a user with BOTH an iOS
        device token AND a linked Telegram must still get gate buttons on
        Telegram — NOT hit the app dead-end where the action silently expires.

        Gate resolution is linked-channel-first, unlike the now-app-first
        ``resolve_user_channel`` used for plain proactive sends: the app has no
        interactive approve/deny surface, so gates never target it while a
        messaging channel is linked."""
        tenant, action = self._make("c33_app_tg", telegram_chat_id=123456789)
        DeviceToken.objects.create(tenant=tenant, user=tenant.user, token="e" * 64, environment="production")

        patcher, tg, line = self._patched_senders(tg=mock.Mock(return_value="777"))
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, True)
        tg.assert_called_once_with(tenant, action)
        line.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "telegram")

    def test_both_channels_linked_routes_to_telegram(self):
        """A user with BOTH Telegram and LINE linked gets gate buttons on
        TELEGRAM — pinning the gate resolver's telegram-before-line order.

        This preserves prior behavior exactly: the old resolver honoured
        ``preferred_channel`` (universally the "telegram" schema default), so
        both-linked users have always received gates on Telegram. The only
        both-linked cohort in production actively approves gates there — a
        line-first order would silently move their approval surface."""
        tenant, action = self._make("c33_both", telegram_chat_id=987654321, line_user_id="U" + "2" * 32)
        # An iOS device token must not perturb the gate order either.
        DeviceToken.objects.create(tenant=tenant, user=tenant.user, token="f" * 64, environment="production")

        patcher, tg, line = self._patched_senders(tg=mock.Mock(return_value="888"))
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, True)
        tg.assert_called_once_with(tenant, action)
        line.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "telegram")
