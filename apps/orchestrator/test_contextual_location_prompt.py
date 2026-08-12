from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.config_generator import (
    _CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK,
    _EVENING_CHECKIN_PROMPT,
    _HEARTBEAT_CHECKIN_PROMPT,
    _HEARTBEAT_CONTEXTUAL_LOCATION_RULE,
    _build_evening_checkin_prompt,
    _build_heartbeat_checkin_prompt,
    _build_morning_briefing_prompt,
    build_cron_seed_jobs,
)
from apps.tenants.services import create_tenant


class ContextualLocationPromptEmissionTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Contextual Location Prompt",
            telegram_chat_id=86120001,
        )

    def _set_flag(self, enabled: bool) -> None:
        self.tenant.situational_context_enabled = enabled
        self.tenant.save(update_fields=["situational_context_enabled"])

    def _proactive_prompts(self) -> dict[str, str]:
        return {
            job["name"]: job["payload"]["message"]
            for job in build_cron_seed_jobs(self.tenant)
            if job["name"] in {"Morning Briefing", "Evening Check-in", "Heartbeat Check-in"}
        }

    def test_flag_on_emits_block_in_evening_and_heartbeat_payloads(self):
        self._set_flag(True)

        prompts = self._proactive_prompts()

        self.assertIn(_CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK, prompts["Evening Check-in"])
        self.assertIn(_CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK, prompts["Heartbeat Check-in"])
        self.assertNotIn(_HEARTBEAT_CONTEXTUAL_LOCATION_RULE, prompts["Evening Check-in"])
        self.assertIn(_HEARTBEAT_CONTEXTUAL_LOCATION_RULE, prompts["Heartbeat Check-in"])

    def test_flag_off_preserves_current_prompt_bodies_byte_for_byte(self):
        self._set_flag(False)

        self.assertEqual(_build_evening_checkin_prompt(self.tenant), _EVENING_CHECKIN_PROMPT)
        self.assertEqual(_build_heartbeat_checkin_prompt(self.tenant), _HEARTBEAT_CHECKIN_PROMPT)
        for prompt in self._proactive_prompts().values():
            self.assertNotIn(_CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK, prompt)

    @patch("apps.orchestrator.config_generator.datebook_delivery_ready", return_value=True)
    def test_flag_off_preserves_datebook_heartbeat_body_byte_for_byte(self, _mock_ready):
        self._set_flag(False)
        expected = _HEARTBEAT_CHECKIN_PROMPT.replace(
            "Calendar — any events in the next 2-3 hours? (`nbhd_calendar_list_events`)",
            "Calendar — call `nbhd_datebook_read` with `days_ahead=0, entity='events'`, "
            "then check the returned times for events in the next 2-3 hours",
        )

        self.assertEqual(_build_heartbeat_checkin_prompt(self.tenant), expected)

    def test_morning_briefing_does_not_emit_contextual_ask_in_either_lane(self):
        for enabled in (False, True):
            with self.subTest(situational_context_enabled=enabled):
                self._set_flag(enabled)
                prompt = _build_morning_briefing_prompt(self.tenant)
                self.assertNotIn(_CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK, prompt)

    def test_block_is_compact_and_keeps_consent_privacy_and_dedup_contracts(self):
        block = _CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK

        self.assertGreaterEqual(len(block), 350)
        self.assertLessEqual(len(block), 500)
        for required in (
            "Conversation so far",
            "recent journal",
            "## Right now",
            "away city with no fresh match or ask in today's note",
            "Sounds like you're in <X>",
            "weather and suggestions",
            "Want things to do nearby?",
            "Log with this check-in's content",
            "nbhd_update_situation",
            "Reply confirms",
            "decline/no reply → no re-ask this trip",
            "vague/no city",
            "fresh match",
            "recorded=home=stated",
            "No sensors/third-party/guesses",
        ):
            self.assertIn(required, block)
