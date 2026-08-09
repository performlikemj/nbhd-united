"""Tests for the Day-0 welcome email + first_message_at tracking."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from apps.router.models import PendingMessage
from apps.router.pending_queue import enqueue_message_for_tenant
from apps.tenants.emails import send_welcome_email
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

User = get_user_model()


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FRONTEND_URL="https://app.example.test",
    DEFAULT_FROM_EMAIL="NBHD United <noreply@example.test>",
    WELCOME_VIDEO_URL="",
)
class WelcomeEmailTests(TestCase):
    def setUp(self):
        mail.outbox = []
        self.tenant = create_tenant(display_name="Alice", telegram_chat_id=970001)
        # create_tenant doesn't set User.email; we need a recipient.
        self.tenant.user.email = "alice@example.test"
        self.tenant.user.save(update_fields=["email"])

    def _make_web_signup_tenant(self) -> Tenant:
        """User who signed up via the web form: no telegram_chat_id."""
        user = User.objects.create_user(
            username="web@example.test",
            email="web@example.test",
            password="x",
            display_name="Web",
        )
        return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)

    # --- Rendered copy ---

    def test_subject_is_ios_first_for_all_signup_paths(self):
        send_welcome_email(self.tenant)
        self.assertEqual(mail.outbox[0].subject, "Welcome — your assistant is ready")

        web = self._make_web_signup_tenant()
        send_welcome_email(web)
        self.assertEqual(mail.outbox[1].subject, "Welcome — your assistant is ready")

    def test_rendered_copy_leads_with_ios_app_and_keeps_channels_secondary(self):
        web = self._make_web_signup_tenant()
        sent = send_welcome_email(web)
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["web@example.test"])
        body_copy = (
            "Your assistant is set up. If you signed up on your iPhone, just open the "
            "NBHD app — your assistant is already saying hello. Prefer messaging apps? "
            "Connect Telegram or LINE from your dashboard and chat there too."
        )
        subhead = "Chat in the NBHD app on your iPhone — or connect Telegram or LINE."
        self.assertIn(subhead, msg.body)
        self.assertIn(body_copy, msg.body)
        self.assertIn("Open the NBHD app", msg.body)
        self.assertIn("https://apps.apple.com/app/id6779158519", msg.body)
        self.assertIn("Connect Telegram or LINE", msg.body)
        self.assertIn("https://app.example.test/settings/integrations", msg.body)

        html = next((b for b, mt in msg.alternatives if mt == "text/html"), "")
        self.assertIn("Your assistant is ready.", html)
        self.assertIn(subhead, html)
        self.assertIn(body_copy, html)
        app_store_position = html.index("https://apps.apple.com/app/id6779158519")
        channels_position = html.index("https://app.example.test/settings/integrations")
        self.assertLess(app_store_position, channels_position)
        self.assertIn("Open the NBHD app", html)
        self.assertIn("Connect Telegram or LINE", html)

    def test_things_to_try_list_is_unchanged(self):
        send_welcome_email(self.tenant)
        msg = mail.outbox[0]
        html = next((b for b, mt in msg.alternatives if mt == "text/html"), "")
        for prompt in (
            '"What can you help me with?"',
            '"I ran 5K this morning"',
            '"Remind me to drink water at 8am every day"',
            '"Hi, who are you?"',
        ):
            self.assertIn(prompt, msg.body)
            self.assertIn(prompt, html)

    # --- Recipient guards ---

    def test_no_recipient_skips_send(self):
        self.tenant.user.email = ""
        self.tenant.user.save(update_fields=["email"])
        sent = send_welcome_email(self.tenant)
        self.assertFalse(sent)
        self.assertEqual(mail.outbox, [])
        self.tenant.refresh_from_db()
        self.assertIsNone(self.tenant.welcome_email_sent_at)

    # --- Idempotency ---

    def test_idempotent_on_second_call(self):
        first = send_welcome_email(self.tenant)
        self.assertTrue(first)
        stamped = Tenant.objects.get(pk=self.tenant.pk).welcome_email_sent_at
        self.assertIsNotNone(stamped)

        second = send_welcome_email(Tenant.objects.get(pk=self.tenant.pk))
        self.assertFalse(second)
        self.assertEqual(len(mail.outbox), 1)
        # Stamp not bumped on the no-op call.
        self.assertEqual(Tenant.objects.get(pk=self.tenant.pk).welcome_email_sent_at, stamped)

    # --- Video URL gating ---

    def test_video_url_empty_omits_walkthrough_block(self):
        send_welcome_email(self.tenant)
        msg = mail.outbox[0]
        self.assertNotIn("walkthrough", msg.body.lower())

    @override_settings(WELCOME_VIDEO_URL="https://youtu.be/example")
    def test_video_url_set_renders_walkthrough_link(self):
        send_welcome_email(self.tenant)
        msg = mail.outbox[0]
        self.assertIn("https://youtu.be/example", msg.body)


class FirstMessageTrackingTests(TestCase):
    """``enqueue_message_for_tenant`` stamps ``first_message_at`` once."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Bob", telegram_chat_id=970002)

    def _enqueue(self, channel: str = "telegram") -> PendingMessage:
        return enqueue_message_for_tenant(
            tenant=self.tenant,
            channel=channel,
            channel_user_id="970002",
            payload={"text": "hi"},
            user_text_excerpt="hi",
        )

    def test_first_inbound_sets_first_message_at(self):
        self.assertIsNone(self.tenant.first_message_at)
        self._enqueue()
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.first_message_at)

    def test_second_inbound_does_not_bump_first_message_at(self):
        self._enqueue()
        self.tenant.refresh_from_db()
        stamped = self.tenant.first_message_at
        self.assertIsNotNone(stamped)

        self._enqueue()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.first_message_at, stamped)

    def test_first_message_tracked_regardless_of_channel(self):
        # Channel string is opaque to the chokepoint — any inbound counts.
        self._enqueue(channel="line")
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.first_message_at)
