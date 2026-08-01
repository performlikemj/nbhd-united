from django.test import SimpleTestCase

from apps.cron.management.commands.register_system_crons import (
    iter_system_crons,
)
from apps.cron.views import TASK_MAP


class StewardRegistrationTests(SimpleTestCase):
    def test_sweep_is_allowlisted_and_self_healing_cron_is_registered(self):
        self.assertEqual(
            TASK_MAP["steward_sweep"],
            "apps.steward.sweep.run_steward_sweep",
        )
        crons = {name: (cron_expr, path, retries) for name, cron_expr, path, retries in iter_system_crons()}
        self.assertEqual(
            crons["steward-sweep"],
            (
                "*/5 * * * *",
                "/api/cron/trigger/steward_sweep/",
                None,
            ),
        )

    def test_daily_digest_is_allowlisted_and_registered(self):
        self.assertEqual(
            TASK_MAP["steward_daily_digest"],
            "apps.steward.digest.run_steward_daily_digest",
        )
        crons = {name: (cron_expr, path, retries) for name, cron_expr, path, retries in iter_system_crons()}
        self.assertEqual(
            crons["steward-daily-digest"],
            (
                "35 22 * * *",
                "/api/cron/trigger/steward_daily_digest/",
                None,
            ),
        )

    def test_collectors_are_allowlisted_and_registered(self):
        self.assertEqual(
            TASK_MAP["steward_collect_github"],
            "apps.steward.tasks.steward_collect_github_task",
        )
        self.assertEqual(
            TASK_MAP["steward_collect_asc"],
            "apps.steward.tasks.steward_collect_asc_task",
        )
        crons = {name: (cron_expr, path, retries) for name, cron_expr, path, retries in iter_system_crons()}
        self.assertEqual(
            crons["steward-collect-github"],
            (
                "*/30 * * * *",
                "/api/cron/trigger/steward_collect_github/",
                None,
            ),
        )
        self.assertEqual(
            crons["steward-collect-asc"],
            (
                "18 * * * *",
                "/api/cron/trigger/steward_collect_asc/",
                None,
            ),
        )
