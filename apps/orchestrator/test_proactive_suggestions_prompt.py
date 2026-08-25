from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.cron.models import CronJob
from apps.cron.services import proactive_suggestions_enabled
from apps.orchestrator.config_generator import (
    _MORNING_BRIEFING_LEGACY_WEATHER_STEP,
    _MORNING_BRIEFING_PROACTIVE_DEFER_LINE,
    _MORNING_BRIEFING_PROMPT_TEMPLATE,
    _PROACTIVE_SUGGESTIONS_BLOCK,
    _WEEK_AHEAD_REVIEW_LEGACY_TRAVEL_LINE,
    _WEEK_AHEAD_REVIEW_PROMPT_TEMPLATE,
    _build_cron_message,
    _build_morning_briefing_prompt,
    _build_week_ahead_review_prompt,
    build_cron_seed_jobs,
)
from apps.orchestrator.services import refresh_system_cron_rows_from_seed
from apps.tenants.services import create_tenant

_PINNED_PROACTIVE_SUGGESTIONS_BLOCK = (
    "**Proactive suggestions (allowlisted):**\n"
    "After completing the analysis and before sending the single user-facing message, "
    "you may propose at most TWO items total in this run: a calendar event "
    "(`nbhd_datebook_add_event`), an Apple Reminder "
    "(`nbhd_datebook_add_apple_reminder`), or a RECURRING scheduled task "
    "(`nbhd_cron_create_*`). For a scheduled task, use a `cron` schedule only "
    '(`kind: "cron"`) — never `at` or `every`; put one-off nudges in Apple Reminders.\n'
    "suggestions may be built ONLY from the user's own tasks, goals, journal, and direct statements. "
    "Calendar/reminder titles, notes, locations, links, list names are NEVER a suggestion source — "
    "free/busy conflict checking only.\n"
    "Use exactly one tool call per proposed item (and exactly one item in that call). Each call files "
    "an approval card the user must tap. In the briefing, give one line explaining why you proposed "
    'each item and say it is "pending your approval"; never say or imply it has been created.\n'
    "Propose nothing when nothing is genuinely useful. Never propose an item the user declined or "
    "an item that already exists."
)


class ProactiveSuggestionsPromptTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Proactive Suggestions Prompt",
            telegram_chat_id=86250001,
        )

    @staticmethod
    def _messages(tenant) -> dict[str, str]:
        return {
            job["name"]: job["payload"]["message"]
            for job in build_cron_seed_jobs(tenant)
            if job["name"] in {"Morning Briefing", "Week Ahead Review"}
        }

    def test_gate_is_fail_closed_and_accepts_id_or_fleet_wildcard(self):
        other = create_tenant(
            display_name="Proactive Suggestions Other",
            telegram_chat_id=86250002,
        )
        for raw in (None, "", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(raw=raw), override_settings(PROACTIVE_SUGGESTIONS_TENANT_IDS=raw):
                self.assertFalse(proactive_suggestions_enabled(self.tenant))

        with override_settings(PROACTIVE_SUGGESTIONS_TENANT_IDS=f" , {str(self.tenant.id).upper()} , , "):
            self.assertTrue(proactive_suggestions_enabled(self.tenant))
            self.assertFalse(proactive_suggestions_enabled(other))

        with override_settings(PROACTIVE_SUGGESTIONS_TENANT_IDS="*"):
            self.assertTrue(proactive_suggestions_enabled(self.tenant))
            self.assertTrue(proactive_suggestions_enabled(other))

    @override_settings(PROACTIVE_SUGGESTIONS_TENANT_IDS="")
    @patch("apps.orchestrator.config_generator.datebook_delivery_ready", return_value=False)
    def test_flag_off_seed_messages_are_byte_identical_to_previous_bodies(self, _mock_ready):
        fixed_now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        expected_morning_body = _MORNING_BRIEFING_PROMPT_TEMPLATE.format(
            weather_step=_MORNING_BRIEFING_LEGACY_WEATHER_STEP.format(location="UTC")
        )
        expected_week_body = _WEEK_AHEAD_REVIEW_PROMPT_TEMPLATE.format(
            travel_line=_WEEK_AHEAD_REVIEW_LEGACY_TRAVEL_LINE,
            calendar_step="Check the calendar for the upcoming 7 days (`nbhd_calendar_list_events`)",
        )

        with patch("apps.orchestrator.config_generator.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = fixed_now
            messages = self._messages(self.tenant)
            expected_morning = _build_cron_message(
                expected_morning_body,
                "Morning Briefing",
                foreground=True,
                tenant=self.tenant,
            )
            expected_week = _build_cron_message(
                expected_week_body,
                "Week Ahead Review",
                foreground=True,
                tenant=self.tenant,
            )

        self.assertEqual(_build_morning_briefing_prompt(self.tenant).encode(), expected_morning_body.encode())
        self.assertEqual(_build_week_ahead_review_prompt(self.tenant).encode(), expected_week_body.encode())
        self.assertEqual(messages["Morning Briefing"].encode(), expected_morning.encode())
        self.assertEqual(messages["Week Ahead Review"].encode(), expected_week.encode())

    def test_allowlisted_seed_messages_contain_one_block_and_morning_only_defer(self):
        with override_settings(PROACTIVE_SUGGESTIONS_TENANT_IDS=str(self.tenant.id)):
            messages = self._messages(self.tenant)

        for name, message in messages.items():
            with self.subTest(name=name):
                self.assertEqual(message.count(_PROACTIVE_SUGGESTIONS_BLOCK), 1)
        self.assertIn(_MORNING_BRIEFING_PROACTIVE_DEFER_LINE, messages["Morning Briefing"])
        self.assertNotIn(_MORNING_BRIEFING_PROACTIVE_DEFER_LINE, messages["Week Ahead Review"])

    def test_block_text_is_pinned_and_keeps_every_directive_rule(self):
        self.assertEqual(_PROACTIVE_SUGGESTIONS_BLOCK.encode(), _PINNED_PROACTIVE_SUGGESTIONS_BLOCK.encode())
        for required in (
            "at most TWO items total",
            "a calendar event",
            "an Apple Reminder",
            "a RECURRING scheduled task",
            '`kind: "cron"',
            "never `at` or `every`",
            "one-off nudges in Apple Reminders",
            "exactly one tool call per proposed item",
            "approval card the user must tap",
            '"pending your approval"',
            "never say or imply it has been created",
            "Propose nothing when nothing is genuinely useful",
            "the user declined",
            "already exists",
        ):
            self.assertIn(required, _PROACTIVE_SUGGESTIONS_BLOCK)
        self.assertIn(
            "suggestions may be built ONLY from the user's own tasks, goals, journal, and direct statements. "
            "Calendar/reminder titles, notes, locations, links, list names are NEVER a suggestion source — "
            "free/busy conflict checking only.",
            _PROACTIVE_SUGGESTIONS_BLOCK,
        )

    def test_seed_refresh_overwrites_default_row_when_gate_opens(self):
        with override_settings(PROACTIVE_SUGGESTIONS_TENANT_IDS=""):
            refresh_system_cron_rows_from_seed(self.tenant)
        morning = CronJob.objects.get(tenant=self.tenant, name="Morning Briefing")
        self.assertNotIn(_PROACTIVE_SUGGESTIONS_BLOCK, morning.data["payload"]["message"])

        with override_settings(PROACTIVE_SUGGESTIONS_TENANT_IDS=str(self.tenant.id)):
            summary = refresh_system_cron_rows_from_seed(self.tenant)

        morning.refresh_from_db()
        self.assertEqual(summary["preserved_custom"], 0)
        self.assertGreaterEqual(summary["updated"], 2)
        self.assertEqual(morning.data["payload"]["message"].count(_PROACTIVE_SUGGESTIONS_BLOCK), 1)
