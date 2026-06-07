"""``refresh_system_cron_rows_from_seed`` reaps managed system crons that have
fallen out of the seed.

When a tenant moves to the built-in heartbeat, ``_build_heartbeat_cron`` returns
None (so "Heartbeat Check-in" leaves the seed); likewise "Gravity Weekly
Check-in" leaves the seed while Gravity is paused. Before this reap those rows
lingered as orphans and kept firing on a stale model. User/custom crons and
platform ``_sync:``/``_fuel:`` crons must be left untouched.
"""

from __future__ import annotations

from django.test import TestCase

from apps.cron.models import CronJob, CronJobSource
from apps.orchestrator.services import refresh_system_cron_rows_from_seed
from apps.tenants.models import Tenant, User


class CronOrphanReapTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reapuser", password="x")
        # Built-in heartbeat ON → "Heartbeat Check-in" is NOT in the seed.
        self.tenant = Tenant.objects.create(
            user=self.user,
            status="active",
            model_tier="starter",
            experimental_built_in_heartbeat=True,
        )

    def _cron(self, name, *, source=CronJobSource.SYSTEM, managed=True):
        return CronJob.objects.create(
            tenant=self.tenant,
            name=name,
            data={"payload": {"message": "x"}},
            source=source,
            managed=managed,
        )

    def test_reaps_orphaned_system_cron_absent_from_seed(self):
        self._cron("Heartbeat Check-in")  # built-in on → not in seed → orphan
        refresh_system_cron_rows_from_seed(self.tenant)
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name="Heartbeat Check-in").exists())

    def test_keeps_system_cron_that_is_in_the_seed(self):
        refresh_system_cron_rows_from_seed(self.tenant)
        # Morning Briefing is always seeded → created and survives the reap.
        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name="Morning Briefing").exists())

    def test_leaves_user_and_platform_managed_crons_alone(self):
        self._cron("My custom reminder", source=CronJobSource.USER)
        self._cron("_fuel:Some Plan")  # system source but platform-managed prefix
        refresh_system_cron_rows_from_seed(self.tenant)
        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name="My custom reminder").exists())
        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name="_fuel:Some Plan").exists())
