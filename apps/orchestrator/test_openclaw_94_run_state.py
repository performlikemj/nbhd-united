"""9.4 rows that have RUN carry root-level run state; it is observation only.

Observed read-only on MJ's 9.4 tenant (2026-09-25): run jobs gain
lastDelivered/lastDeliveryStatus/lastFailureNotificationDeliveryStatus/
lastRunAtMs/lastRunStatus at the top level. The contract fixtures never fired
(cron disabled), so they never showed these.
"""

import json
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

from apps.orchestrator.cron_declarations import supported_declaration
from apps.orchestrator.migration_preservation import declaration_digest

RUN_STATE = {
    "lastDelivered": False,
    "lastDeliveryStatus": "not-requested",
    "lastFailureNotificationDeliveryStatus": "not-requested",
    "lastRunAtMs": 1790319600187,
    "lastRunStatus": "ok",
}


def ran_row():
    return {
        "id": "runtime-id",
        "name": "Morning Briefing",
        "declarationKey": "nbhd:1",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "payload": {
            "kind": "agentTurn",
            "message": "Synthetic",
            "model": "openrouter/deepseek/deepseek-v4-flash-0731",
            "toolsAllow": ["*"],
        },
        "delivery": {"mode": "none", "channel": "last"},
        "scheduledToolPolicy": {"version": 1, "mode": "trusted"},
        **RUN_STATE,
    }


class RunStateTests(SimpleTestCase):
    def test_run_state_is_observation_not_declaration(self):
        self.assertTrue(supported_declaration(ran_row()))
        fresh = {k: v for k, v in ran_row().items() if k not in RUN_STATE}
        self.assertEqual(declaration_digest(ran_row()), declaration_digest(fresh))

    def test_unknown_root_field_still_refuses(self):
        self.assertFalse(supported_declaration({**ran_row(), "futureControl": True}))

    @skipUnless(shutil.which("node"), "Node is required for the JS mirror")
    def test_js_mirror_agrees(self):
        compare = (Path(__file__).parent / "migration_cron_compare.mjs").resolve().as_uri()
        rows = [ran_row(), {**ran_row(), "futureControl": True}]
        script = f"""
import {{supportedDeclaration}} from {json.dumps(compare)};
console.log(JSON.stringify({json.dumps(rows)}.map(supportedDeclaration)));
"""
        out = subprocess.run(
            ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(json.loads(out), [True, False])

    def test_python_and_js_field_lists_match(self):
        root = Path(__file__).parent
        fields = json.loads((root / "cron-declaration-fields.json").read_text())
        source = (root / "migration_cron_compare.mjs").read_text()
        for name in fields["observation"]:
            self.assertIn(f'"{name}"', source)
