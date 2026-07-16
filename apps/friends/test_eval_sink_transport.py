"""Eval-sink isolation across friend notifications on all transports."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase, override_settings

from apps.friends.models import Friendship, NeighborProfile
from apps.router.models import DeviceToken
from apps.tenants.models import Tenant, User


@override_settings(
    TELEGRAM_BOT_TOKEN="test-telegram-token",
    LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    APNS_AUTH_KEY="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
    APNS_KEY_ID="ABC1234567",
    APNS_TEAM_ID="TEAM123456",
    APNS_BUNDLE_ID="org.hoodunited.nbhd",
)
class EvalSinkFriendNotificationTest(TestCase):
    def test_high3_wave_never_emits_telegram_line_or_apns(self):
        requester_user = User.objects.create_user(username="friend_eval_requester", password="x")
        requester = Tenant.objects.create(user=requester_user, status=Tenant.Status.ACTIVE)
        NeighborProfile.objects.create(tenant=requester, handle="requester", display_name="Requester")

        recipient_user = User.objects.create_user(
            username="friend_eval_recipient",
            password="x",
            telegram_chat_id=987654,
            line_user_id="U_friend_eval_recipient",
            preferred_channel="telegram",
        )
        recipient = Tenant.objects.create(
            user=recipient_user,
            status=Tenant.Status.ACTIVE,
            is_synthetic=True,
            is_eval_sink=True,
        )
        DeviceToken.objects.create(tenant=recipient, user=recipient_user, token="c" * 64)
        friendship = Friendship.objects.create(
            requester=requester,
            addressee=recipient,
            status=Friendship.Status.PENDING,
        )

        from apps.friends.services import _notify_wave_received

        with (
            mock.patch("apps.friends.notifications.notify_wave_received") as wave_transport,
            mock.patch("apps.friends.notifications.notify_wave_app") as app_transport,
        ):
            _notify_wave_received(friendship)
        wave_transport.assert_not_called()
        app_transport.assert_not_called()

        from apps.friends.notifications import notify_wave_app, notify_wave_received

        with (
            mock.patch("httpx.post") as transport_post,
            mock.patch("apps.common.apns.send_push") as send_push,
        ):
            self.assertFalse(notify_wave_received(friendship))
            notify_wave_app(friendship)

        transport_post.assert_not_called()
        send_push.assert_not_called()
