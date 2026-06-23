"""Tests for the `send_assistant_ready_email` management command."""

from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase

from .models import Tenant

User = get_user_model()


def _user(email, *, name="", status=Tenant.Status.ACTIVE, with_tenant=True):
    u = User.objects.create_user(username=email, email=email, password="x", display_name=name)
    if with_tenant:
        Tenant.objects.create(user=u, status=status)
    return u


class SendAssistantReadyEmailTests(TestCase):
    def test_sends_to_active_user_with_expected_content(self):
        _user("jacky@example.com", name="Jacky")
        out = StringIO()
        call_command("send_assistant_ready_email", "--email", "jacky@example.com", stdout=out)

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.subject, "Your assistant is ready")
        self.assertEqual(msg.to, ["jacky@example.com"])
        self.assertIn("Hi Jacky,", msg.body)
        self.assertIn("That's fixed", msg.body)
        # HTML alternative is attached with the branded shell.
        html = msg.alternatives[0][0]
        self.assertIn("Your assistant is live.", html)
        self.assertIn("Open NBHD", html)

    def test_junk_display_name_falls_back_to_there(self):
        _user("t@example.com", name="test")
        call_command("send_assistant_ready_email", "--email", "t@example.com")
        self.assertIn("Hi there,", mail.outbox[0].body)

    def test_skips_non_active_tenant_without_force(self):
        _user("pending@example.com", name="P", status=Tenant.Status.PENDING)
        out = StringIO()
        call_command("send_assistant_ready_email", "--email", "pending@example.com", stdout=out)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("SKIP", out.getvalue())

    def test_skips_user_with_no_tenant_without_force(self):
        _user("notenant@example.com", with_tenant=False)
        call_command("send_assistant_ready_email", "--email", "notenant@example.com")
        self.assertEqual(len(mail.outbox), 0)

    def test_force_sends_to_non_active_tenant(self):
        _user("pending2@example.com", name="P", status=Tenant.Status.PENDING)
        call_command("send_assistant_ready_email", "--email", "pending2@example.com", "--force")
        self.assertEqual(len(mail.outbox), 1)

    def test_dry_run_sends_nothing(self):
        _user("dry@example.com", name="Dry")
        out = StringIO()
        call_command("send_assistant_ready_email", "--email", "dry@example.com", "--dry-run", stdout=out)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("[dry-run]", out.getvalue())

    def test_unknown_email_errors(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("send_assistant_ready_email", "--email", "nobody@example.com")
