"""Tests for platform system notifications (apps/router/system_notify.py).

Regression coverage for the app-first outbound routing change: a token-holding
tenant (iOS device registered, no Telegram/LINE) must still receive a system
notice — e.g. a model-health switch — via a ``ProactiveOutbound`` row (APNs push
+ ?since= feed). Before the app branch existed, ``send_system_notification``
returned False for these users and the notice silently vanished.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from apps.router.models import DeviceToken, ProactiveOutbound
from apps.router.system_notify import send_system_notification
from apps.tenants.models import Tenant, User


class SystemNotifyAppBranchTest(TestCase):
    def _tenant(self, username, **user_kwargs):
        user = User.objects.create_user(username=username, email=f"{username}@example.com", **user_kwargs)
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
        return tenant

    def test_app_channel_notice_records_proactive_outbound(self):
        tenant = self._tenant("sysnotify_app")
        DeviceToken.objects.create(tenant=tenant, user=tenant.user, token="a" * 64)
        with mock.patch("apps.router.proactive_context._dispatch_ios_push") as push:
            ok = send_system_notification(tenant, "Your assistant switched models.")

        self.assertTrue(ok)
        row = ProactiveOutbound.objects.get(tenant=tenant)
        self.assertEqual(row.channel, "app")
        self.assertEqual(row.message_text, "Your assistant switched models.")
        self.assertEqual(row.channel_user_id, str(tenant.user_id))
        push.assert_called_once()

    def test_no_channel_notice_skipped(self):
        tenant = self._tenant("sysnotify_none")
        ok = send_system_notification(tenant, "nobody home")
        self.assertFalse(ok)
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=tenant).exists())

    def test_telegram_notice_does_not_record_row(self):
        # Linked Telegram user without a device: still delivered over Telegram,
        # NOT the app branch — no ProactiveOutbound row is written for the
        # channel-send path (that path already handled it pre-change).
        tenant = self._tenant("sysnotify_tg", telegram_chat_id=555)
        with mock.patch("apps.router.system_notify._send_telegram", return_value=True) as tg:
            ok = send_system_notification(tenant, "hi")

        self.assertTrue(ok)
        tg.assert_called_once()
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=tenant).exists())
