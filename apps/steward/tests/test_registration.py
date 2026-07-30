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
