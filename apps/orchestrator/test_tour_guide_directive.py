"""Per-tenant tour-guide gate, mode-selected docs, and capability flip command."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.orchestrator.personas import render_workspace_files
from apps.orchestrator.tour_guide import (
    TOUR_GUIDE_CONTRACT_CARDS,
    TOUR_GUIDE_CONTRACT_LINKS,
    TOUR_GUIDE_CONTRACT_MAX_CHARS,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

_TOUR_GUIDE_MARKER = "## Tour guide"
_TOUR_GUIDE_DOC_CUE = "read `docs/tour-guide.md` THIS TURN"
_TOUR_GUIDE_TOOL_CUE = "call `nbhd_tour_guide` FIRST this turn"
_JOURNAL_SHAPING_MARKER = "## Journal shaping"
_RECONCILED_OPENCLAW_VERSION = "2026.5.28"
_FORMER_GATE_OPENCLAW_VERSION = "2026.5.29"
_LEGACY_TOUR_GUIDE_GATE = (
    "## Tour guide\n\n"
    "When the user asks what to do, where to eat, or how to spend time around a place — "
    'or any message contains a "📍 Current location" line — read `docs/tour-guide.md` '
    "THIS TURN, before answering, and follow its reply format exactly. Never ask where "
    "the user is when a recent 📍 message exists."
)


def _agents_md(tenant) -> str:
    return render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]


class TourGuideGateTest(TestCase):
    def test_flag_on_tenant_gets_imperative_gate(self):
        tenant = create_tenant(display_name="Tour Guide On", telegram_chat_id=943001)
        tenant.tour_guide_enabled = True
        tenant.save(update_fields=["tour_guide_enabled"])

        agents_md = _agents_md(tenant)

        self.assertIn(_TOUR_GUIDE_MARKER, agents_md)
        self.assertIn(_TOUR_GUIDE_DOC_CUE, agents_md)
        self.assertIn("Never ask where the user is", agents_md)

    def test_flag_off_tenant_does_not_get_gate(self):
        tenant = create_tenant(display_name="Tour Guide Off", telegram_chat_id=943002)

        self.assertFalse(tenant.tour_guide_enabled)
        agents_md = _agents_md(tenant)
        self.assertNotIn(_TOUR_GUIDE_MARKER, agents_md)
        self.assertNotIn(_TOUR_GUIDE_DOC_CUE, agents_md)

    def test_manifest_ok_with_reconciled_version_gets_tool_response_gate(self):
        tenant = create_tenant(display_name="Tour Guide Tool Gate", telegram_chat_id=943006)
        tenant.tour_guide_enabled = True
        tenant.tour_guide_manifest_ok = True
        tenant.openclaw_version = _RECONCILED_OPENCLAW_VERSION
        tenant.save(update_fields=["tour_guide_enabled", "tour_guide_manifest_ok", "openclaw_version"])

        agents_md = _agents_md(tenant)

        self.assertIn(_TOUR_GUIDE_TOOL_CUE, agents_md)
        self.assertIn("follow the contract in its response exactly", agents_md)
        self.assertNotIn(_TOUR_GUIDE_DOC_CUE, agents_md)

    def test_manifest_ok_disabled_tenant_gets_no_tour_guide_gate(self):
        tenant = create_tenant(display_name="Tour Guide Tool Off", telegram_chat_id=943007)
        tenant.tour_guide_manifest_ok = True
        tenant.openclaw_version = _RECONCILED_OPENCLAW_VERSION
        tenant.save(update_fields=["tour_guide_manifest_ok", "openclaw_version"])

        agents_md = _agents_md(tenant)

        self.assertNotIn(_TOUR_GUIDE_MARKER, agents_md)
        self.assertNotIn("nbhd_tour_guide", agents_md)

    def test_manifest_not_ok_agents_md_is_byte_identical_to_pre_tool_output(self):
        tenant = create_tenant(display_name="Tour Guide Unverified Manifest", telegram_chat_id=943008)
        tenant.openclaw_version = _FORMER_GATE_OPENCLAW_VERSION
        self.assertFalse(tenant.tour_guide_manifest_ok)
        tenant.journal_shaping_enabled = True
        tenant.save(update_fields=["openclaw_version", "journal_shaping_enabled"])
        post_1247_without_tour_guide = _agents_md(tenant)

        journal_boundary = "\n\n" + _JOURNAL_SHAPING_MARKER
        self.assertIn(journal_boundary, post_1247_without_tour_guide)
        expected_current_main = post_1247_without_tour_guide.replace(
            journal_boundary,
            "\n\n" + _LEGACY_TOUR_GUIDE_GATE + journal_boundary,
            1,
        )

        tenant.tour_guide_enabled = True
        tenant.save(update_fields=["tour_guide_enabled"])

        rendered = _agents_md(tenant)
        self.assertLess(rendered.index(_TOUR_GUIDE_MARKER), rendered.index(_JOURNAL_SHAPING_MARKER))
        self.assertIn(_TOUR_GUIDE_DOC_CUE, rendered)
        self.assertNotIn(_TOUR_GUIDE_TOOL_CUE, rendered)
        self.assertEqual(rendered.encode(), expected_current_main.encode())

    def test_tool_gate_is_shorter_than_legacy_doc_read_gate(self):
        tenant = create_tenant(display_name="Tour Guide Gate Diet", telegram_chat_id=943009)
        tenant.tour_guide_enabled = True
        tenant.save(update_fields=["tour_guide_enabled"])
        legacy_gate = _agents_md(tenant).split(_TOUR_GUIDE_MARKER, 1)[1]

        tenant.tour_guide_manifest_ok = True
        tenant.save(update_fields=["tour_guide_manifest_ok"])
        tool_gate = _agents_md(tenant).split(_TOUR_GUIDE_MARKER, 1)[1]

        self.assertLess(len(tool_gate), len(legacy_gate))

    def test_same_gate_is_used_for_both_modes(self):
        tenant = create_tenant(display_name="Tour Guide Modes", telegram_chat_id=943003)
        tenant.tour_guide_enabled = True
        gates = []
        for mode in Tenant.TourGuideMode:
            tenant.tour_guide_mode = mode
            tenant.save(update_fields=["tour_guide_enabled", "tour_guide_mode"])
            rendered = _agents_md(tenant)
            gates.append(rendered[rendered.index(_TOUR_GUIDE_MARKER) :])

        self.assertEqual(gates[0], gates[1])

    def test_gate_block_is_under_defensive_length_guard(self):
        tenant = create_tenant(display_name="Tour Guide Lean", telegram_chat_id=943004)
        tenant.tour_guide_enabled = True
        tenant.save(update_fields=["tour_guide_enabled"])

        agents_md = _agents_md(tenant)
        gate = agents_md[agents_md.index(_TOUR_GUIDE_MARKER) :]

        self.assertLessEqual(len(gate), 700)

    def test_config_is_writable_with_flag_on(self):
        tenant = create_tenant(display_name="Tour Guide Config", telegram_chat_id=943005)
        tenant.tour_guide_enabled = True
        tenant.save(update_fields=["tour_guide_enabled"])

        assert_config_writable(generate_openclaw_config(tenant))


class TourGuideDocTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Tour Guide Docs", telegram_chat_id=943010)
        self.files = render_workspace_files("neighbor", tenant=self.tenant)

    def test_cards_doc_contains_nbhd_guide_contract(self):
        cards = self.files["NBHD_DOC_TOUR_GUIDE_CARDS"]

        self.assertIn("## Recommendations — itinerary cards", cards)
        self.assertIn("```nbhd-guide", cards)
        self.assertIn('"v": 1', cards)
        self.assertIn("EXACTLY ONE fenced code block", cards)

    def test_links_doc_has_plain_links_and_no_fences(self):
        links = self.files["NBHD_DOC_TOUR_GUIDE_LINKS"]

        self.assertIn("## Recommendations — links", links)
        self.assertIn("**bold place name**", links)
        self.assertIn("maps link on its own line", links)
        self.assertNotIn("```", links)


class TourGuideToolContractTest(TestCase):
    def test_contracts_are_present_and_bounded(self):
        for contract in (TOUR_GUIDE_CONTRACT_CARDS, TOUR_GUIDE_CONTRACT_LINKS):
            self.assertTrue(contract)
            self.assertLess(len(contract), TOUR_GUIDE_CONTRACT_MAX_CHARS)

    def test_cards_contract_carries_load_bearing_rules(self):
        self.assertIn("nbhd-guide", TOUR_GUIDE_CONTRACT_CARDS)
        self.assertIn("NEVER draw", TOUR_GUIDE_CONTRACT_CARDS)
        self.assertIn("walking order", TOUR_GUIDE_CONTRACT_CARDS)

    def test_contracts_carry_offering_rules(self):
        for contract in (TOUR_GUIDE_CONTRACT_CARDS, TOUR_GUIDE_CONTRACT_LINKS):
            with self.subTest(contract=contract):
                self.assertIn("OFFERING", contract)
                self.assertIn("offer once", contract)
                self.assertIn("do not offer again", contract)
                self.assertIn("Never offer at home", contract)
                self.assertIn("lone 📍 message away from home", contract)
                self.assertIn("lone 📍 near home", contract)

    def test_contracts_carry_grounding_rules(self):
        for contract in (TOUR_GUIDE_CONTRACT_CARDS, TOUR_GUIDE_CONTRACT_LINKS):
            with self.subTest(contract=contract):
                self.assertIn("never abbreviate", contract)
                self.assertIn("verify with a quick web search", contract)

    def test_links_contract_carries_plain_maps_link_rules(self):
        self.assertNotIn("```", TOUR_GUIDE_CONTRACT_LINKS)
        self.assertIn("Never emit fenced code blocks", TOUR_GUIDE_CONTRACT_LINKS)
        self.assertIn("maps.apple.com", TOUR_GUIDE_CONTRACT_LINKS)


class TourGuideToolConfigTest(TestCase):
    @staticmethod
    def _settings_tools_entry(tenant) -> dict:
        return generate_openclaw_config(tenant)["plugins"]["entries"]["nbhd-settings-tools"]

    @staticmethod
    def _compact_bytes(value: dict) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    def test_manifest_ok_with_reconciled_version_emits_contract_and_slim_gate(self):
        tenant = create_tenant(display_name="Tour Guide Tool Config", telegram_chat_id=943011)
        tenant.tour_guide_enabled = True
        tenant.tour_guide_manifest_ok = True
        tenant.openclaw_version = _RECONCILED_OPENCLAW_VERSION

        expected_contracts = {
            Tenant.TourGuideMode.CARDS: TOUR_GUIDE_CONTRACT_CARDS,
            Tenant.TourGuideMode.LINKS: TOUR_GUIDE_CONTRACT_LINKS,
        }
        for mode, expected_contract in expected_contracts.items():
            with self.subTest(mode=mode):
                tenant.tour_guide_mode = mode
                entry = self._settings_tools_entry(tenant)
                self.assertEqual(
                    entry["config"],
                    {
                        "tourGuideEnabled": True,
                        "tourGuideMode": mode,
                        "tourGuideContract": expected_contract,
                    },
                )
                agents_md = _agents_md(tenant)
                self.assertIn(_TOUR_GUIDE_TOOL_CUE, agents_md)
                self.assertNotIn(_TOUR_GUIDE_DOC_CUE, agents_md)

    def test_manifest_ok_emits_disabled_flag_but_persona_gate_stays_absent(self):
        tenant = create_tenant(display_name="Tour Guide Tool Disabled", telegram_chat_id=943012)
        tenant.tour_guide_manifest_ok = True
        tenant.openclaw_version = _RECONCILED_OPENCLAW_VERSION

        entry = self._settings_tools_entry(tenant)

        self.assertFalse(entry["config"]["tourGuideEnabled"])
        self.assertNotIn(_TOUR_GUIDE_MARKER, _agents_md(tenant))

    def test_manifest_not_ok_settings_tools_config_is_byte_identical_to_today(self):
        tenant = create_tenant(display_name="Tour Guide Config Unverified", telegram_chat_id=943013)
        tenant.openclaw_version = _FORMER_GATE_OPENCLAW_VERSION
        self.assertFalse(tenant.tour_guide_manifest_ok)
        tenant.journal_shaping_enabled = True
        tenant.save(update_fields=["openclaw_version", "journal_shaping_enabled"])

        before_config = generate_openclaw_config(tenant)
        before = self._compact_bytes(before_config["plugins"])
        self.assertEqual(
            before_config["plugins"]["entries"]["nbhd-journal-shaping"]["config"],
            {"journalShapingEnabled": True},
        )
        self.assertEqual(self._settings_tools_entry(tenant), {"enabled": True})
        self.assertNotIn(b"tourGuide", before)

        tenant.tour_guide_enabled = True
        tenant.tour_guide_mode = Tenant.TourGuideMode.CARDS
        after_config = generate_openclaw_config(tenant)
        after = self._compact_bytes(after_config["plugins"])

        self.assertEqual(after, before)
        self.assertNotIn(b"tourGuide", after)

    def test_manifest_ok_enabled_config_is_writable(self):
        tenant = create_tenant(display_name="Tour Guide Tool Writable", telegram_chat_id=943014)
        tenant.tour_guide_enabled = True
        tenant.tour_guide_manifest_ok = True
        tenant.openclaw_version = _RECONCILED_OPENCLAW_VERSION
        tenant.tour_guide_mode = Tenant.TourGuideMode.CARDS

        assert_config_writable(generate_openclaw_config(tenant))


class TourGuideServiceSelectionTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Tour Guide Upload", telegram_chat_id=943020)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-tour-guide-test"
        self.tenant.tour_guide_enabled = True
        self.tenant.save(update_fields=["status", "container_id", "tour_guide_enabled"])

    def _uploaded_tour_guide(self) -> list[str]:
        with (
            patch("apps.orchestrator.services.upload_config_to_file_share"),
            patch("apps.orchestrator.services.config_to_json", return_value="{}"),
            patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}}),
            patch("apps.orchestrator.services._audit_and_log"),
            patch(
                "apps.orchestrator.services.refresh_system_cron_rows_from_seed",
                return_value={"created": 0, "updated": 0, "preserved_custom": 0, "unchanged": 0},
            ),
            patch("apps.orchestrator.azure_client.download_workspace_file", return_value=None),
            patch("apps.orchestrator.azure_client.upload_workspace_file") as upload_workspace_file,
            patch("apps.orchestrator.workspace_envelope.push_user_md"),
        ):
            from apps.orchestrator.services import update_tenant_config

            update_tenant_config(str(self.tenant.id))

        return [
            call.args[2]
            for call in upload_workspace_file.call_args_list
            if call.args[1] == "workspace/docs/tour-guide.md"
        ]

    def test_cards_mode_uploads_cards_doc_to_stable_destination(self):
        self.tenant.tour_guide_mode = Tenant.TourGuideMode.CARDS
        self.tenant.save(update_fields=["tour_guide_mode"])

        uploads = self._uploaded_tour_guide()

        self.assertEqual(len(uploads), 1)
        self.assertIn("```nbhd-guide", uploads[0])

    def test_links_mode_uploads_links_doc_to_stable_destination(self):
        self.tenant.tour_guide_mode = Tenant.TourGuideMode.LINKS
        self.tenant.save(update_fields=["tour_guide_mode"])

        uploads = self._uploaded_tour_guide()

        self.assertEqual(len(uploads), 1)
        self.assertIn("## Recommendations — links", uploads[0])
        self.assertNotIn("```", uploads[0])


class SetTourGuideCommandTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Tour Guide Command", telegram_chat_id=943030)

    def _call(self, *args) -> str:
        stdout = io.StringIO()
        call_command("set_tour_guide", *args, stdout=stdout)
        return stdout.getvalue()

    def test_enable_sets_mode_and_bumps_pending_config(self):
        initial_version = self.tenant.pending_config_version

        output = self._call(
            "--tenant-id",
            str(self.tenant.id),
            "--enable",
            "--mode",
            "cards",
        )

        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.tour_guide_enabled)
        self.assertEqual(self.tenant.tour_guide_mode, Tenant.TourGuideMode.CARDS)
        self.assertEqual(self.tenant.pending_config_version, initial_version + 1)
        self.assertIn("tour_guide_enabled=True", output)
        self.assertIn("force_apply_configs --tenant-id", output)

    def test_disable_and_switch_to_links(self):
        self.tenant.tour_guide_enabled = True
        self.tenant.tour_guide_mode = Tenant.TourGuideMode.CARDS
        self.tenant.save(update_fields=["tour_guide_enabled", "tour_guide_mode"])

        self._call(
            "--tenant-id",
            str(self.tenant.id),
            "--disable",
            "--mode",
            "links",
        )

        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.tour_guide_enabled)
        self.assertEqual(self.tenant.tour_guide_mode, Tenant.TourGuideMode.LINKS)

    def test_manifest_flags_update_readiness_and_bump_pending_config(self):
        initial_version = self.tenant.pending_config_version

        output = self._call(
            "--tenant-id",
            str(self.tenant.id),
            "--manifest-ok",
        )

        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.tour_guide_manifest_ok)
        self.assertEqual(self.tenant.pending_config_version, initial_version + 1)
        self.assertIn("tour_guide_manifest_ok=True", output)

        self._call(
            "--tenant-id",
            str(self.tenant.id),
            "--manifest-not-ok",
        )

        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.tour_guide_manifest_ok)
        self.assertEqual(self.tenant.pending_config_version, initial_version + 2)

    def test_unknown_tenant_errors(self):
        with self.assertRaises(CommandError):
            self._call(
                "--tenant-id",
                "00000000-0000-0000-0000-000000000000",
                "--enable",
            )
