"""Routine crons must run on the fast, non-reasoning worker model — not the
slow reasoning "leader" (DeepSeek V4 Pro).

Guards the 2026-06 cron-timeout fix: DeepSeek on the cron path overshot the
per-turn ceiling and timed out, so routine/scheduled crons were moved to the
small/fast Gemma worker model. This pins that so a future edit can't quietly
re-introduce a slow reasoning model for a scheduled cron.
"""

from __future__ import annotations

from django.test import TestCase

from apps.billing.constants import DEEPSEEK_MODEL, REASONING_MODELS
from apps.orchestrator.config_generator import HEARTBEAT_MODEL, TIER_TASK_DEFAULTS

# Crons that were timing out on the reasoning model — each must carry an
# explicit fast default rather than falling through to the tenant's chat
# primary (which may itself be the reasoning model).
_REQUIRED_FAST_SLUGS = {
    "morning_briefing",
    "evening_checkin",
    "weekly_reflection",
    "week_review",
    "project_checkin",
    "gravity_weekly_checkin",
    "personal_question",
    "background_tasks",
}


class RoutineCronModelTests(TestCase):
    def test_no_routine_cron_defaults_to_a_reasoning_model(self):
        for tier, slugs in TIER_TASK_DEFAULTS.items():
            for slug, model in slugs.items():
                self.assertNotEqual(model, DEEPSEEK_MODEL, f"{tier}/{slug} is back on the slow reasoning leader")
                self.assertNotIn(model, REASONING_MODELS, f"{tier}/{slug} is on a reasoning model")

    def test_heartbeat_runs_on_a_fast_model(self):
        self.assertNotEqual(HEARTBEAT_MODEL, DEEPSEEK_MODEL)
        self.assertNotIn(HEARTBEAT_MODEL, REASONING_MODELS)

    def test_timeout_prone_crons_carry_an_explicit_fast_default(self):
        starter = TIER_TASK_DEFAULTS.get("starter", {})
        missing = _REQUIRED_FAST_SLUGS - set(starter)
        self.assertEqual(missing, set(), f"these crons would inherit the chat primary: {missing}")
