"""Audit cluster C03 regression tests — rotate_all_passwords recovery path.

Covers FA-1071 / FA-1085 / FA-1140: a user whose reset email fails during
the rotation campaign is rotated (unusable password) but never re-emailed by
a same-``--since`` retry. The new ``--resend-only`` flag re-sends the reset
link to already-rotated users without re-bumping ``password_last_changed_at``.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tenants.models import Tenant, User


def _make_user(*, email: str) -> User:
    user = User.objects.create(username=email, email=email, display_name="Test")
    user.set_password("pw-initial")
    user.save()
    Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        is_trial=True,
        trial_ends_at=timezone.now() + timedelta(days=3),
    )
    return user


@override_settings(PLATFORM_OWNER_EMAIL="owner@test.com")
class ResendOnlyRecoveryTests(TestCase):
    def setUp(self):
        self.alice = _make_user(email="alice@test.com")
        self.bob = _make_user(email="bob@test.com")

    def test_resend_only_recovers_email_failed_user(self):
        """A user rotated but not emailed (transient send failure) is locked out;
        --resend-only re-sends their reset link without re-bumping the stamp."""
        cutoff = timezone.now()

        # First run: bob's email send fails for everyone EXCEPT alice.
        real_send_mail = mail.send_mail

        def flaky_send(*args, **kwargs):
            recipients = kwargs.get("recipient_list") or (args[3] if len(args) > 3 else [])
            if "bob@test.com" in recipients:
                raise RuntimeError("simulated Mailgun outage")
            return real_send_mail(*args, **kwargs)

        with patch(
            "apps.tenants.management.commands.rotate_all_passwords.send_mail",
            side_effect=flaky_send,
        ):
            call_command("rotate_all_passwords", reason="first", since=cutoff.isoformat())

        # Both rotated (unusable), only alice got an email.
        self.alice.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertFalse(self.alice.has_usable_password())
        self.assertFalse(self.bob.has_usable_password())
        self.assertEqual({m.to[0] for m in mail.outbox}, {"alice@test.com"})

        bob_stamp_before = self.bob.password_last_changed_at

        # A naive same---since retry skips bob (already rotated above cutoff).
        call_command("rotate_all_passwords", reason="retry", since=cutoff.isoformat())
        self.assertEqual({m.to[0] for m in mail.outbox}, {"alice@test.com"})

        # --resend-only re-sends bob's reset link without re-bumping the stamp.
        mail.outbox.clear()
        call_command("rotate_all_passwords", reason="resend", resend_only=True)

        recipients = {m.to[0] for m in mail.outbox}
        self.assertIn("bob@test.com", recipients)
        self.assertIn("alice@test.com", recipients)  # both have unusable pw now

        self.bob.refresh_from_db()
        self.assertEqual(self.bob.password_last_changed_at, bob_stamp_before)
        self.assertFalse(self.bob.has_usable_password())

    def test_resend_only_skips_usable_password_users(self):
        """Never-rotated users (usable password) are not emailed by --resend-only."""
        # No prior rotation — both alice and bob still have usable passwords.
        call_command("rotate_all_passwords", reason="resend", resend_only=True)
        self.assertEqual(len(mail.outbox), 0)

    def test_resend_only_excludes_owner(self):
        owner = _make_user(email="owner@test.com")
        owner.set_unusable_password()
        owner.save(update_fields=["password", "password_last_changed_at"])

        # Rotate alice so she is a resend candidate.
        self.alice.set_unusable_password()
        self.alice.save(update_fields=["password", "password_last_changed_at"])

        call_command("rotate_all_passwords", reason="resend", resend_only=True)
        recipients = {m.to[0] for m in mail.outbox}
        self.assertNotIn("owner@test.com", recipients)
        self.assertIn("alice@test.com", recipients)
