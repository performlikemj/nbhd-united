"""Tests for the stale device-token revocation command."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, time, timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.router.models import DeviceToken
from apps.tenants.models import Tenant, User


class RevokeStaleDeviceTokensTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username=f"stale_push_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-stale-push.example.com",
        )
        self.cutoff = timezone.now() - timedelta(days=7)

    def _token(self, token: str, *, last_seen_at, revoked_at=None, environment="production"):
        row = DeviceToken.objects.create(
            user=self.user,
            tenant=self.tenant,
            token=token,
            revoked_at=revoked_at,
            environment=environment,
        )
        DeviceToken.objects.filter(pk=row.pk).update(last_seen_at=last_seen_at)
        row.refresh_from_db()
        return row

    def _run(self, cutoff, *args):
        stdout = StringIO()
        call_command(
            "revoke_stale_device_tokens",
            "--last-seen-before",
            cutoff,
            *args,
            stdout=stdout,
        )
        return stdout.getvalue()

    def test_revokes_stale_unrevoked_token_and_leaves_fresh_token(self):
        stale = self._token("a" * 64, last_seen_at=self.cutoff - timedelta(days=1), environment="sandbox")
        fresh = self._token("b" * 64, last_seen_at=self.cutoff + timedelta(days=1))

        output = self._run(self.cutoff.isoformat())

        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertIsNotNone(stale.revoked_at)
        self.assertIsNone(fresh.revoked_at)
        self.assertIn("Total targeted: 1", output)
        self.assertIn("sandbox: 1", output)
        self.assertIn("production: 0", output)
        self.assertIn("Distinct users affected: 1", output)
        self.assertIn(f"id={stale.id}", output)
        self.assertNotIn(stale.token, output)
        self.assertIn("revoked 1 tokens", output)

    def test_already_revoked_stale_token_is_not_restamped(self):
        original_revoked_at = self.cutoff - timedelta(days=2)
        row = self._token(
            "c" * 64,
            last_seen_at=self.cutoff - timedelta(days=1),
            revoked_at=original_revoked_at,
        )

        output = self._run(self.cutoff.isoformat())

        row.refresh_from_db()
        self.assertEqual(row.revoked_at, original_revoked_at)
        self.assertIn("Total targeted: 0", output)
        self.assertIn("revoked 0 tokens", output)

    def test_dry_run_reports_target_without_changing_it_and_accepts_date(self):
        cutoff_date = (timezone.now() - timedelta(days=7)).date()
        cutoff = datetime.combine(cutoff_date, time.min, tzinfo=UTC)
        stale = self._token("d" * 64, last_seen_at=cutoff - timedelta(days=1))

        output = self._run(cutoff_date.isoformat(), "--dry-run")

        stale.refresh_from_db()
        self.assertIsNone(stale.revoked_at)
        self.assertIn("Total targeted: 1", output)
        self.assertIn("production: 1", output)
        self.assertIn("DRY RUN — nothing changed", output)

    def test_future_cutoff_is_rejected(self):
        future = timezone.now() + timedelta(days=1)

        with self.assertRaises(CommandError):
            self._run(future.isoformat())
