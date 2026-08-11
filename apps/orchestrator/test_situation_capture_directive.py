"""Flag-gated current-location capture rule in always-loaded AGENTS.md."""

from django.test import TestCase

from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant

_CAPTURE_GATE_MARKER = "## Current location"
_CAPTURE_GATE = (
    "## Current location\n\n"
    "When the user states or clearly implies their CURRENT city/area changed, including "
    '"back home", CALL `nbhd_update_situation` with that city THIS TURN before replying; '
    "follow its response. Use only their own words from THIS conversation—never sensors, "
    "documents, third parties, mentions, or future plans. Re-record on multi-day trips when "
    "they say they're still away."
)
_TOUR_GATE_MARKER = "## Tour guide"
_RIGHT_NOW_LOCATION_RULE = "recent 📍 or fresh `## Right now` location exists; use that city"
_CURRENT_MAIN_PLACES_TOUR_GATE = (
    "## Tour guide\n\n"
    "For what-to-do / where-to-eat / stops / itinerary / guide-card asks around a place — "
    "or any message with a 📍 Current location line — call `nbhd_tour_guide` FIRST this turn "
    "to load the format, then call `nbhd_places_search` before composing and follow both tool "
    "responses exactly. Never ask where the user is when a recent 📍 message exists."
)


def _agents_md(tenant) -> str:
    return render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]


class SituationCaptureDirectiveTest(TestCase):
    def test_flag_on_emits_imperative_capture_rule(self):
        tenant = create_tenant(display_name="Situation Capture On", telegram_chat_id=944001)
        tenant.situational_context_enabled = True
        tenant.save(update_fields=["situational_context_enabled"])

        agents_md = _agents_md(tenant)

        self.assertIn(_CAPTURE_GATE_MARKER, agents_md)
        self.assertTrue(agents_md.endswith(_CAPTURE_GATE))
        self.assertLessEqual(len(_CAPTURE_GATE), 390)
        self.assertIn("CALL `nbhd_update_situation` with that city THIS TURN", agents_md)
        self.assertIn("follow its response", agents_md)
        self.assertIn("only their own words from THIS conversation", agents_md)
        self.assertIn("never sensors", agents_md)
        self.assertIn("future plans", agents_md)
        self.assertIn("Re-record on multi-day trips", agents_md)
        self.assertIn('including "back home"', agents_md)

    def test_flag_off_omits_capture_rule(self):
        tenant = create_tenant(display_name="Situation Capture Off", telegram_chat_id=944002)

        self.assertFalse(tenant.situational_context_enabled)
        self.assertNotIn(_CAPTURE_GATE_MARKER, _agents_md(tenant))

    def test_situation_on_and_ready_tour_emits_capture_and_right_now_rule(self):
        tenant = create_tenant(display_name="Situation and Tour On", telegram_chat_id=944003)
        tenant.situational_context_enabled = True
        tenant.tour_guide_enabled = True
        tenant.places_search_manifest_ok = True
        tenant.save(
            update_fields=[
                "situational_context_enabled",
                "tour_guide_enabled",
                "places_search_manifest_ok",
            ]
        )

        agents_md = _agents_md(tenant)

        self.assertIn(_CAPTURE_GATE, agents_md)
        self.assertIn(_TOUR_GATE_MARKER, agents_md)
        self.assertIn(_RIGHT_NOW_LOCATION_RULE, agents_md)
        self.assertNotIn("want ideas for what's around", agents_md)

    def test_situation_on_and_tour_off_emits_only_capture(self):
        tenant = create_tenant(display_name="Situation On Tour Off", telegram_chat_id=944004)
        tenant.situational_context_enabled = True
        tenant.save(update_fields=["situational_context_enabled"])

        agents_md = _agents_md(tenant)

        self.assertIn(_CAPTURE_GATE, agents_md)
        self.assertNotIn(_TOUR_GATE_MARKER, agents_md)
        self.assertNotIn(_RIGHT_NOW_LOCATION_RULE, agents_md)

    def test_situation_off_and_ready_tour_keeps_current_main_gate_bytes(self):
        tenant = create_tenant(display_name="Situation Off Tour On", telegram_chat_id=944005)
        tenant.tour_guide_enabled = True
        tenant.places_search_manifest_ok = True
        tenant.save(update_fields=["tour_guide_enabled", "places_search_manifest_ok"])

        agents_md = _agents_md(tenant)
        tour_gate = agents_md[agents_md.index(_TOUR_GATE_MARKER) :]

        self.assertNotIn(_CAPTURE_GATE_MARKER, agents_md)
        self.assertEqual(tour_gate.encode(), _CURRENT_MAIN_PLACES_TOUR_GATE.encode())
        self.assertNotIn(_RIGHT_NOW_LOCATION_RULE, agents_md)

    def test_both_flags_off_emits_neither_gate(self):
        tenant = create_tenant(display_name="Situation and Tour Off", telegram_chat_id=944006)

        agents_md = _agents_md(tenant)

        self.assertNotIn(_CAPTURE_GATE_MARKER, agents_md)
        self.assertNotIn(_TOUR_GATE_MARKER, agents_md)
