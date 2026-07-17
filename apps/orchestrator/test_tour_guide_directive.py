"""Per-tenant tour-guide gate, mode-selected docs, and capability flip command."""

from __future__ import annotations

import io
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

_TOUR_GUIDE_MARKER = "## Tour guide"
_TOUR_GUIDE_DOC_CUE = "read `docs/tour-guide.md` THIS TURN"


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

    def test_unknown_tenant_errors(self):
        with self.assertRaises(CommandError):
            self._call(
                "--tenant-id",
                "00000000-0000-0000-0000-000000000000",
                "--enable",
            )
