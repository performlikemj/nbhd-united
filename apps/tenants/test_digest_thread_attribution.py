"""Tenant capability flag and command tests for digest thread attribution."""

from __future__ import annotations

import io

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.router.conversation_capture import digest_thread_attribution_enabled
from apps.tenants.models import Tenant, User


class DigestThreadAttributionFlagTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="digest_attr_flag", password="pw")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)

    def test_default_is_disabled_and_shared_helper_fail_closes(self):
        self.assertFalse(self.tenant.digest_thread_attribution_enabled)
        self.assertFalse(digest_thread_attribution_enabled(self.tenant))
        self.assertFalse(digest_thread_attribution_enabled(None))

    def test_shared_helper_reads_dedicated_field(self):
        self.tenant.digest_thread_attribution_enabled = True

        self.assertTrue(digest_thread_attribution_enabled(self.tenant))


class SetDigestThreadAttributionCommandTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="digest_attr_command", password="pw")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)

    def _call(self, *args) -> str:
        stdout = io.StringIO()
        call_command("set_digest_thread_attribution", *args, stdout=stdout)
        return stdout.getvalue()

    def test_enable_sets_flag_and_bumps_pending_config(self):
        initial_version = self.tenant.pending_config_version

        output = self._call("--tenant-id", str(self.tenant.id), "--enable")

        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.digest_thread_attribution_enabled)
        self.assertEqual(self.tenant.pending_config_version, initial_version + 1)
        self.assertIn("digest_thread_attribution_enabled=True", output)
        self.assertIn("force_apply_configs --tenant-id", output)

    def test_disable_clears_flag(self):
        self.tenant.digest_thread_attribution_enabled = True
        self.tenant.save(update_fields=["digest_thread_attribution_enabled"])

        self._call("--tenant-id", str(self.tenant.id), "--disable")

        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.digest_thread_attribution_enabled)

    def test_unknown_tenant_errors(self):
        with self.assertRaises(CommandError):
            self._call(
                "--tenant-id",
                "00000000-0000-0000-0000-000000000000",
                "--enable",
            )
