"""Pin pre-panel prompt bytes for tenants outside the rollout allowlist."""

import hashlib
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from apps.cron.patterns.daily_briefing import DailyBriefingHandler, DailyBriefingPayload
from apps.orchestrator.config_generator import _build_morning_briefing_prompt
from apps.router.panels import MORNING_PANEL_INSTRUCTION, PANEL_KINDS
from apps.tenants.models import Tenant, User


class MorningPanelPromptTests(SimpleTestCase):
    def setUp(self):
        self.tenant = Tenant(
            id=UUID("00000000-0000-4000-8000-000000000111"),
            user=User(timezone="Asia/Tokyo", location_city="Osaka"),
        )

    def typed(self):
        return DailyBriefingHandler().build_oc_data(
            DailyBriefingPayload(),
            tenant=self.tenant,
            name="Morning Briefing",
            schedule={"kind": "cron", "expr": "0 7 * * *", "tz": "Asia/Tokyo"},
        )["payload"]

    @override_settings(CHAT_SHAPE_TENANT_IDS="")
    def test_non_gated_prompts_match_pre_change_bytes(self):
        # Captured from the unmodified base before implementing panels.
        for message, digest in (
            (
                _build_morning_briefing_prompt(self.tenant),
                "971a2d208ea892278913a5781da5bc41977e6c72500ba79fa68b4235a98d8ccd",
            ),
            (self.typed()["message"], "ba590434878b1fbb42708f7e6e816eb758a4b70774d13aa1db812d6cd91245c5"),
        ):
            self.assertEqual(hashlib.sha256(message.encode()).hexdigest(), digest)
            self.assertNotIn("attach panels", message)
        self.assertNotIn("nbhd_fuel_summary", self.typed()["toolsAllow"])

    def test_both_gated_prompts_include_references_and_read_only_evidence_tool(self):
        with override_settings(CHAT_SHAPE_TENANT_IDS=str(self.tenant.id)):
            typed = self.typed()
            for message in (_build_morning_briefing_prompt(self.tenant), typed["message"]):
                self.assertEqual(message.count(MORNING_PANEL_INSTRUCTION), 1)
                for kind in PANEL_KINDS:
                    self.assertIn(kind, message)
                for phrase in (
                    "last_night",
                    "due_today",
                    "body_weight",
                    "this_month",
                    "Keep prose short",
                    "Omit cards without evidence",
                ):
                    self.assertIn(phrase, message)
            self.assertIn("nbhd_fuel_summary", typed["toolsAllow"])
            self.assertNotIn("nbhd_fuel_log_sleep", typed["toolsAllow"])
