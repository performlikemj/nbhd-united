"""App-path (iOS) delivery tests for wave notifications.

Phase 1 of the channel decommission (see CONTINUITY_channel_decommission.md)
adds an app path to ``notify_wave_received``: a device-holding addressee now
gets a notification-only APNs push + ?since= feed row via
``record_proactive_outbound`` (no accept/decline buttons — an iOS-parity
follow-up per decision D2), instead of the Telegram/LINE-only dispatch that
silently vanished for iOS-only users.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from apps.router.models import DeviceToken, ProactiveOutbound
from apps.tenants.models import Tenant, User

from .models import Friendship, NeighborProfile
from .notifications import notify_wave_received


def _tenant(username: str) -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status="active", friends_enabled=True)


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class WaveAppDeliveryTest(TestCase):
    def test_device_holder_gets_app_notification(self):
        waver = _tenant("wave_from")
        NeighborProfile.objects.create(tenant=waver, handle="waver", display_name="Waver")
        addressee = _tenant("wave_to")
        DeviceToken.objects.create(tenant=addressee, user=addressee.user, token="e" * 64, environment="production")
        friendship = Friendship.objects.create(
            requester=waver,
            addressee=addressee,
            status=Friendship.Status.PENDING,
            invite_note="hi there",
        )

        self.assertTrue(notify_wave_received(friendship))

        row = ProactiveOutbound.objects.get(tenant=addressee)
        self.assertEqual(row.channel, "app")
        self.assertEqual(row.channel_user_id, str(addressee.user_id))
        self.assertIn("Waver", row.message_text)

    def test_no_device_no_delivery(self):
        waver = _tenant("wave_from2")
        addressee = _tenant("wave_to2")
        friendship = Friendship.objects.create(requester=waver, addressee=addressee, status=Friendship.Status.PENDING)

        # No device and no Telegram/LINE link → nothing delivered.
        self.assertFalse(notify_wave_received(friendship))
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=addressee).exists())
