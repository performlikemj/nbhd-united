"""Pin pre-panel prompt bytes for tenants outside the rollout allowlist."""

import hashlib
import json
from unittest.mock import patch
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from apps.billing.models import FreeModelOffer
from apps.cron.patterns.daily_briefing import DailyBriefingHandler, DailyBriefingPayload
from apps.orchestrator.config_generator import _build_morning_briefing_prompt, generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.router.panels import MORNING_PANEL_INSTRUCTION, PANEL_KINDS
from apps.tenants.models import Tenant, User


@override_settings(CHAT_PANELS_TOOL_TENANT_IDS="", CHAT_SHAPE_TENANT_IDS="")
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

    @override_settings(CHAT_PANELS_TOOL_TENANT_IDS="")
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

    def test_shape_only_prompts_are_byte_identical_to_non_gated(self):
        before = _build_morning_briefing_prompt(self.tenant), self.typed()
        with override_settings(CHAT_SHAPE_TENANT_IDS=str(self.tenant.id)):
            self.test_non_gated_prompts_match_pre_change_bytes()
            self.assertEqual((_build_morning_briefing_prompt(self.tenant), self.typed()), before)

    def test_tool_only_prompts_match_both_gates(self):
        with override_settings(CHAT_PANELS_TOOL_TENANT_IDS=str(self.tenant.id)):
            before = _build_morning_briefing_prompt(self.tenant), self.typed()
            with override_settings(CHAT_SHAPE_TENANT_IDS=str(self.tenant.id)):
                self.assertEqual((_build_morning_briefing_prompt(self.tenant), self.typed()), before)
        self.assertIn(MORNING_PANEL_INSTRUCTION, before[0])

    def test_both_gated_prompts_include_references_and_read_only_evidence_tool(self):
        with override_settings(
            CHAT_SHAPE_TENANT_IDS=str(self.tenant.id), CHAT_PANELS_TOOL_TENANT_IDS=str(self.tenant.id)
        ):
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
                    "Put panels ONLY in the panels argument, never in message text",
                ):
                    self.assertIn(phrase, message)
            self.assertIn("nbhd_fuel_summary", typed["toolsAllow"])
            self.assertNotIn("nbhd_fuel_log_sleep", typed["toolsAllow"])


@override_settings(
    CHAT_SHAPE_TENANT_IDS="",
    CHAT_PANELS_TOOL_TENANT_IDS="",
    OPENCLAW_JOURNAL_PLUGIN_ID="nbhd-journal-tools",
    OPENCLAW_JOURNAL_PLUGIN_PATH="/opt/nbhd/plugins/nbhd-journal-tools",
)
class PanelToolConfigTests(SimpleTestCase):
    def setUp(self):
        self.tenant = Tenant(
            id=UUID("00000000-0000-4000-8000-000000000111"),
            user=User(timezone="Asia/Tokyo", location_city="Osaka"),
            openclaw_version="2026.9.4",
        )
        self.enterContext(patch("apps.orchestrator.config_generator._byo_model_extras", return_value=[]))
        self.enterContext(patch("apps.billing.model_offers._offer", return_value=FreeModelOffer(enabled=False)))
        self.enterContext(patch("django.db.models.query.QuerySet.exists", return_value=False))

    @staticmethod
    def config_bytes(config):
        return json.dumps(config, sort_keys=True, separators=(",", ":")).encode()

    def test_off_gate_entire_config_matches_pre_review_bytes(self):
        # An old canary must remain safe even when shape is enabled.
        self.tenant.container_image_tag = "2026.9.4-cronfix"
        # Captured from 1ee74fb0 before changing config generation in this round.
        for gate in ("", "00000000-0000-4000-8000-000000000222", str(self.tenant.id)):
            with self.subTest(gate=gate), override_settings(CHAT_SHAPE_TENANT_IDS=gate):
                config = generate_openclaw_config(self.tenant)
            self.assertEqual(
                hashlib.sha256(self.config_bytes(config)).hexdigest(),
                "d88c6883522e3f126a377b1a64067375989325041a6782501ce1591fa03cf7d5",
            )
            self.assertEqual(config["plugins"]["entries"]["nbhd-journal-tools"], {"enabled": True})
            assert_config_writable(config)

    def test_gated_config_changes_only_journal_tool_flag_and_validates(self):
        with override_settings(CHAT_SHAPE_TENANT_IDS=""):
            before = generate_openclaw_config(self.tenant)
        with override_settings(
            CHAT_SHAPE_TENANT_IDS=str(self.tenant.id), CHAT_PANELS_TOOL_TENANT_IDS=str(self.tenant.id)
        ):
            after = generate_openclaw_config(self.tenant)
        self.assertEqual(after["plugins"]["entries"]["nbhd-journal-tools"]["config"], {"panelsEnabled": True})
        assert_config_writable(after)
        with override_settings(CHAT_PANELS_TOOL_TENANT_IDS=str(self.tenant.id)):
            tool_only = generate_openclaw_config(self.tenant)
        self.assertEqual(self.config_bytes(tool_only), self.config_bytes(after))
        after["plugins"]["entries"]["nbhd-journal-tools"].pop("config")
        self.assertEqual(self.config_bytes(after), self.config_bytes(before))
