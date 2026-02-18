from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from apps.tenants.models import Tenant, User


def _create_tenant(status=Tenant.Status.ACTIVE, *, suffix: int = 0):
    user = User.objects.create_user(
        username=f"tenant-cmd-{status.lower()}-{suffix}",
        password="pass1234",
    )
    return Tenant.objects.create(
        user=user,
        status=status,
        model_tier=Tenant.ModelTier.STARTER,
    )


class BumpConfigVersionCommandTest(TestCase):
    def test_bump_command_increments_active_tenants_only(self):
        active = _create_tenant(Tenant.Status.ACTIVE, suffix=1)
        inactive = _create_tenant(Tenant.Status.SUSPENDED, suffix=2)

        call_command("bump_config_version")

        active.refresh_from_db()
        inactive.refresh_from_db()
        self.assertEqual(active.pending_config_version, 1)
        self.assertEqual(inactive.pending_config_version, 0)

    def test_bump_command_targets_single_tenant(self):
        target = _create_tenant(Tenant.Status.ACTIVE, suffix=3)
        other = _create_tenant(Tenant.Status.ACTIVE, suffix=4)

        call_command("bump_config_version", tenant=str(other.id))

        target.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(target.pending_config_version, 0)
        self.assertEqual(other.pending_config_version, 1)
