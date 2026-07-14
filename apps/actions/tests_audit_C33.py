"""Regression tests for FA-0006 (cluster C33), updated for channel decommission.

``send_gate_confirmation`` previously read ``tenant.user.preferred_channel``
directly and indexed ``_SENDERS`` (which maps only ``telegram``/``line``).
``preferred_channel`` defaults to ``"telegram"`` even for iOS-only App Store
users (DeviceToken but no telegram_chat_id/line_user_id), so the Telegram sender
ran, failed deep with a misleading "no Telegram chat_id" log, and the gate
action silently expired with no prompt ever delivered.

The C33 fix routed channel resolution through ``resolve_user_channel``. Phase 1
of the channel decommission (see ``CONTINUITY_channel_decommission.md``) narrows
that resolver to app-or-nothing: a device-holding user now gets a
notification-only app push (ProactiveOutbound row + APNs), and a Telegram/LINE
link with no device is no longer a delivery surface. The core C33 invariant is
preserved and strengthened: the Telegram/LINE senders are never invoked.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.actions import messaging
from apps.actions.messaging import send_gate_confirmation
from apps.actions.models import ActionType, PendingAction
from apps.router.models import DeviceToken, ProactiveOutbound
from apps.tenants.models import Tenant

User = get_user_model()


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
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

    def test_ios_only_user_routes_to_app_notification(self):
        """iOS-only user (DeviceToken, no telegram/line link, default
        preferred_channel='telegram') must NOT reach the Telegram sender and
        gets a notification-only app delivery (ProactiveOutbound row)."""
        tenant, action = self._make("c33_ios")
        self.assertEqual(tenant.user.preferred_channel, "telegram")
        self.assertIsNone(tenant.user.telegram_chat_id)
        DeviceToken.objects.create(tenant=tenant, user=tenant.user, token="a" * 64, environment="production")

        patcher, tg, line = self._patched_senders()
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, True)
        tg.assert_not_called()
        line.assert_not_called()

        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "app")
        row = ProactiveOutbound.objects.get(tenant=tenant)
        self.assertEqual(row.channel, "app")

    def test_token_less_user_routes_to_app_notification(self):
        """A user with neither a messaging channel nor a DeviceToken still has the
        app/console surface: the gate is delivered notification-only via the app,
        the Telegram/LINE senders are never invoked, and a ProactiveOutbound row
        is written (the APNs push is a best-effort no-op with zero tokens)."""
        tenant, action = self._make("c33_none")

        patcher, tg, line = self._patched_senders()
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, True)
        tg.assert_not_called()
        line.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "app")
        self.assertTrue(ProactiveOutbound.objects.filter(tenant=tenant, channel="app").exists())

    def test_telegram_linked_with_device_routes_to_app(self):
        """A Telegram-linked user who also has a device routes to the app — the
        Telegram sender is never invoked (decommission Phase 1)."""
        tenant, action = self._make("c33_tg_dev", telegram_chat_id=123456789)
        DeviceToken.objects.create(tenant=tenant, user=tenant.user, token="c" * 64, environment="production")

        patcher, tg, line = self._patched_senders(tg=mock.Mock(return_value="555"))
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, True)
        tg.assert_not_called()
        line.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "app")

    def test_telegram_linked_without_device_is_undeliverable(self):
        """A Telegram link with no registered device is no longer a delivery
        surface: nothing is sent and the action is left un-delivered."""
        tenant, action = self._make("c33_tg_only", telegram_chat_id=123456789)

        patcher, tg, line = self._patched_senders(tg=mock.Mock(return_value="555"))
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, False)
        tg.assert_not_called()
        line.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "")
