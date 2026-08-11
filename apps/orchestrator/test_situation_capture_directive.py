"""Flag-gated current-location capture rule in always-loaded AGENTS.md."""

from django.test import TestCase

from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant

_CAPTURE_GATE_MARKER = "## Current-location capture"


def _agents_md(tenant) -> str:
    return render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]


class SituationCaptureDirectiveTest(TestCase):
    def test_flag_on_emits_imperative_capture_rule(self):
        tenant = create_tenant(display_name="Situation Capture On", telegram_chat_id=944001)
        tenant.situational_context_enabled = True
        tenant.save(update_fields=["situational_context_enabled"])

        agents_md = _agents_md(tenant)

        self.assertIn(_CAPTURE_GATE_MARKER, agents_md)
        self.assertIn("CALL `nbhd_update_situation` with that city label THIS TURN", agents_md)
        self.assertIn("acknowledge the capture in one short clause", agents_md)
        self.assertIn("Use only the user's own words in THIS conversation", agents_md)
        self.assertIn("never record a place merely mentioned", agents_md)
        self.assertIn("Record future travel only once", agents_md)
        self.assertIn("re-record when the user references still being away", agents_md)
        self.assertIn('"Back home" or equivalent means record the home city', agents_md)
        self.assertIn("If the user objects, do not record it again; drop the subject", agents_md)

    def test_flag_off_omits_capture_rule(self):
        tenant = create_tenant(display_name="Situation Capture Off", telegram_chat_id=944002)

        self.assertFalse(tenant.situational_context_enabled)
        self.assertNotIn(_CAPTURE_GATE_MARKER, _agents_md(tenant))
