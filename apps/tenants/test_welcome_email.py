"""Tests for the Day-0 welcome email + first_message_at tracking."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.router.models import LineQuotaState, PendingMessage
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

    # --- Telegram-connected branch ---

    def test_telegram_connected_email_references_telegram_chat(self):
        sent = send_welcome_email(self.tenant)
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["alice@example.test"])
        self.assertIn("Hi Alice", msg.body)
        self.assertIn("Telegram", msg.body)
        # Telegram-connected branch does NOT include the CTA pill copy.
        self.assertNotIn("Connect Telegram or LINE", msg.body)

    def test_telegram_connected_html_uses_gradient_hero(self):
        send_welcome_email(self.tenant)
        msg = mail.outbox[0]
        html = next((b for b, mt in msg.alternatives if mt == "text/html"), "")
        # Hero copy is in both the MSO fallback and the gradient span.
        self.assertIn("Your assistant is ready.", html)
        # The Constellation eyebrow.
        self.assertIn("NBHD", html)

    # --- Web-signup branch (no chat_id) ---

    def test_web_signup_email_links_to_settings_integrations(self):
        web = self._make_web_signup_tenant()
        sent = send_welcome_email(web)
        self.assertTrue(sent)
        msg = mail.outbox[0]
        self.assertIn("https://app.example.test/settings/integrations", msg.body)
        html = next((b for b, mt in msg.alternatives if mt == "text/html"), "")
        # CTA pill copy appears only on the web-signup branch.
        self.assertIn("Connect Telegram or LINE", html)

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

    # --- LINE quota gating ---

    def test_line_quota_exhausted_hides_line_postscript(self):
        LineQuotaState.objects.update_or_create(
            pk=1,
            defaults={
                "line_quota_limit": 1000,
                "line_quota_used": 1000,
                "line_quota_exhausted_at": timezone.now(),
            },
        )
        send_welcome_email(self.tenant)
        msg = mail.outbox[0]
        html = next((b for b, mt in msg.alternatives if mt == "text/html"), "")
        # Telegram-connected + exhausted quota → no LINE P.S.
        self.assertNotIn("also use your assistant on LINE", msg.body)
        self.assertNotIn("also use your assistant on LINE", html)

    def test_line_quota_available_shows_line_postscript_for_telegram_user(self):
        # No LineQuotaState row → get_or_create returns one with
        # line_quota_exhausted_at=None → is_exhausted=False.
        send_welcome_email(self.tenant)
        msg = mail.outbox[0]
        self.assertIn("also use your assistant on LINE", msg.body)

    def test_line_quota_postscript_omitted_for_web_signup(self):
        # Web-signup gets the CTA pill, not the P.S. — both routes lead
        # to the same dashboard URL so duplicating would be noise.
        web = self._make_web_signup_tenant()
        send_welcome_email(web)
        msg = mail.outbox[0]
        self.assertNotIn("also use your assistant on LINE", msg.body)

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
