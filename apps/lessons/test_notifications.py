"""App-path (iOS) delivery tests for lesson approval notifications.

Phase 1 of the channel decommission (see CONTINUITY_channel_decommission.md)
adds an app path to ``send_lesson_approval_buttons``: a device-holding user now
gets a notification-only APNs push + ?since= feed row via
``record_proactive_outbound`` (no approve/skip buttons — an iOS-parity follow-up
per decision D2), instead of the Telegram/LINE-only dispatch that silently
vanished for iOS-only users.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from apps.router.models import DeviceToken, ProactiveOutbound
from apps.tenants.models import Tenant, User

from .models import Lesson
from .notifications import send_lesson_approval_buttons


def _tenant(username: str) -> Tenant:
    user = User.objects.create_user(username=username, password="pass")
    return Tenant.objects.create(user=user, status="active")


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class LessonAppDeliveryTest(TestCase):
    def test_device_holder_gets_app_notification(self):
        tenant = _tenant("lesson_ios")
        DeviceToken.objects.create(tenant=tenant, user=tenant.user, token="f" * 64, environment="production")
        lesson = Lesson.objects.create(tenant=tenant, text="Rest is productive.", source_type="reflection")

        self.assertTrue(send_lesson_approval_buttons(tenant, lesson))

        row = ProactiveOutbound.objects.get(tenant=tenant)
        self.assertEqual(row.channel, "app")
        self.assertEqual(row.channel_user_id, str(tenant.user_id))
        self.assertIn("Rest is productive.", row.message_text)

    def test_no_device_no_delivery(self):
        tenant = _tenant("lesson_none")
        lesson = Lesson.objects.create(tenant=tenant, text="x", source_type="reflection")

        # No device and no Telegram/LINE link → nothing delivered.
        self.assertFalse(send_lesson_approval_buttons(tenant, lesson))
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=tenant).exists())
