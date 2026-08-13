from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.config_generator import (
    _CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK,
    _EVENING_CHECKIN_PROMPT,
    _HEARTBEAT_CHECKIN_PROMPT,
    _HEARTBEAT_CONTEXTUAL_LOCATION_RULE,
    _MORNING_BRIEFING_AWAY_TOUR_PILL_BLOCK,
    _MORNING_BRIEFING_LEGACY_WEATHER_STEP,
    _MORNING_BRIEFING_PROMPT_TEMPLATE,
    _MORNING_BRIEFING_WEATHER_STEP,
    _build_evening_checkin_prompt,
    _build_heartbeat_checkin_prompt,
    _build_morning_briefing_prompt,
    _with_morning_briefing_away_tour_pill,
    build_cron_seed_jobs,
)
from apps.tenants.services import create_tenant

_PINNED_CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK = (
    "**Location:** USER.md: compare the user's own `Conversation so far` + recent journal "
    "with `## Right now`. Named away city with no fresh match or ask in today's note → "
    "weave in once: “Sounds like you're in <X>—want me to use it for weather and suggestions "
    "here? Want things to do nearby?” Log with this check-in's content. Reply confirms → "
    "`nbhd_update_situation`(X); decline/no reply → no re-ask this trip. Silent: vague/no "
    "city, fresh match or recorded=home=stated. No sensors/third-party/guesses."
)
_PINNED_MORNING_BRIEFING_WEATHER_STEP = (
    "1. Weather city: check `## Right now` in USER.md — if it shows a fresh Current location, "
    "get today's weather with `web_search` for that city. The value below is a SNAPSHOT of "
    "the home base taken when this job was created and may be stale; use it only if "
    "`## Right now` shows nothing fresher. SNAPSHOT home base: {location}. Get the weather "
    'with `web_search` for "<city> weather forecast today" (a follow-up search for tomorrow '
    "is fine). Do NOT use web_fetch, curl, or exec — none of those "
    "are available; web_search is the only weather tool you have.\n"
)
_PINNED_MORNING_BRIEFING_LEGACY_WEATHER_STEP = (
    '1. Get today\'s weather with `web_search` for "{location} weather forecast today" '
    '(a follow-up search — e.g. "{location} weather tomorrow" — is fine if the first '
    "result doesn't cover tomorrow). Do NOT use web_fetch, curl, or exec — none of those "
    "are available; web_search is the only weather tool you have.\n"
)


class ContextualLocationPromptEmissionTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Contextual Location Prompt",
            telegram_chat_id=86120001,
        )

    def _set_flag(self, enabled: bool) -> None:
        self.tenant.situational_context_enabled = enabled
        self.tenant.save(update_fields=["situational_context_enabled"])

    def _set_tour(self, *, enabled: bool, readiness_field: str | None = None) -> None:
        self.tenant.tour_guide_enabled = enabled
        self.tenant.places_search_manifest_ok = False
        self.tenant.tour_guide_manifest_ok = False
        update_fields = [
            "tour_guide_enabled",
            "places_search_manifest_ok",
            "tour_guide_manifest_ok",
        ]
        if readiness_field is not None:
            setattr(self.tenant, readiness_field, True)
        self.tenant.save(update_fields=update_fields)

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

    def test_morning_briefing_situation_on_and_ready_tour_emits_away_tour_pill(self):
        for readiness_field in ("places_search_manifest_ok", "tour_guide_manifest_ok"):
            with self.subTest(readiness_field=readiness_field):
                self._set_flag(True)
                self._set_tour(enabled=True, readiness_field=readiness_field)

                prompt = _build_morning_briefing_prompt(self.tenant)

                self.assertIn(_MORNING_BRIEFING_AWAY_TOUR_PILL_BLOCK, prompt)
                self.assertIn(
                    _MORNING_BRIEFING_WEATHER_STEP.format(location="UTC"),
                    prompt,
                )

    def test_morning_briefing_situation_on_without_ready_tour_is_byte_identical(self):
        for tour_enabled in (False, True):
            with self.subTest(tour_enabled=tour_enabled):
                self._set_flag(True)
                self._set_tour(enabled=tour_enabled)
                weather_step = _MORNING_BRIEFING_WEATHER_STEP.format(location="UTC")

                prompt = _build_morning_briefing_prompt(self.tenant)
                expected = _MORNING_BRIEFING_PROMPT_TEMPLATE.format(weather_step=weather_step)

                self.assertEqual(prompt.encode(), expected.encode())
                self.assertNotIn(_MORNING_BRIEFING_AWAY_TOUR_PILL_BLOCK, prompt)

    def test_morning_briefing_situation_off_is_byte_identical_for_both_tour_states(self):
        self._set_flag(False)
        self._set_tour(enabled=False)
        tour_off_prompt = _build_morning_briefing_prompt(self.tenant)
        weather_step = _MORNING_BRIEFING_LEGACY_WEATHER_STEP.format(location="UTC")
        expected = _MORNING_BRIEFING_PROMPT_TEMPLATE.format(weather_step=weather_step)

        self._set_tour(enabled=True, readiness_field="places_search_manifest_ok")
        tour_on_prompt = _build_morning_briefing_prompt(self.tenant)

        self.assertEqual(tour_off_prompt.encode(), expected.encode())
        self.assertEqual(tour_on_prompt.encode(), tour_off_prompt.encode())
        self.assertNotIn(_MORNING_BRIEFING_AWAY_TOUR_PILL_BLOCK, tour_on_prompt)

    def test_morning_briefing_away_tour_pill_insertion_fails_loudly(self):
        self._set_flag(True)
        self._set_tour(enabled=True, readiness_field="places_search_manifest_ok")

        with self.assertRaisesRegex(ValueError, "no marker-contract insertion point"):
            _with_morning_briefing_away_tour_pill("anchor missing", self.tenant)

    def test_contextual_ask_and_weather_steps_are_byte_pinned(self):
        self.assertEqual(
            _CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK.encode(),
            _PINNED_CONTEXTUAL_LOCATION_CONFIRM_ASK_BLOCK.encode(),
        )
        self.assertEqual(
            _MORNING_BRIEFING_WEATHER_STEP.encode(),
            _PINNED_MORNING_BRIEFING_WEATHER_STEP.encode(),
        )
        self.assertEqual(
            _MORNING_BRIEFING_LEGACY_WEATHER_STEP.encode(),
            _PINNED_MORNING_BRIEFING_LEGACY_WEATHER_STEP.encode(),
        )

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
