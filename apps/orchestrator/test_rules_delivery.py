"""Rules-delivery boundaries for chat prompts and cron sessions."""

from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from apps.orchestrator.config_generator import _prepare_cron_prompt
from apps.orchestrator.personas import PERSONAS, render_workspace_files
from apps.orchestrator.test_reminder_capability import MaximalTenantBudgetTest
from apps.tenants.services import create_tenant

_ROOT = Path(__file__).resolve().parents[2]
_TOOLS_REFERENCE = _ROOT / "templates/openclaw/docs/tools-reference.md"
_TOOLS_REFERENCE_HEADER = (
    "<!-- CRON-ONLY: chat sessions cannot read this file; it is listed in the cron preamble. "
    "Tool descriptions are the authoritative reference. -->"
)

_CHAT_FILE_POINTER = re.compile(r"(?:rules|docs)/[a-z_-]+\.md")
_CRON_INDEX_ROWS = (
    ("rules/journal-capture.md", "Journal capture"),
    ("rules/lessons-constellation.md", "Lessons"),
    ("rules/messaging.md", "Cron delivery, check-in windows, automated routines"),
    ("rules/week-ahead.md", "Weekly review"),
    ("rules/fuel.md", "Fuel"),
    ("docs/tools-reference.md", "before using any tool you're unsure about"),
    ("docs/cron-management.md", "before creating, editing, or disabling scheduled tasks"),
    ("docs/error-handling.md", "when a tool fails or a feature isn't working"),
)


@override_settings(GRAVITY_ENABLED=True)
class RulesDeliveryTest(TestCase):
    def _all_gates_tenant(self):
        budget_case = MaximalTenantBudgetTest(methodName="test_maximal_render_stays_under_the_cap")
        return budget_case._tenant(all_gates=True)

    def test_chat_prompt_has_no_file_pointers(self):
        all_gates_tenant = self._all_gates_tenant()

        for persona_key in PERSONAS:
            for shape, tenant in (("lean", None), ("all-gates", all_gates_tenant)):
                with self.subTest(persona=persona_key, shape=shape):
                    prompt = render_workspace_files(persona_key, tenant=tenant)["NBHD_AGENTS_MD"]
                    self.assertIsNone(
                        _CHAT_FILE_POINTER.search(prompt),
                        f"{persona_key} {shape} chat prompt points at a workspace file",
                    )

    def test_cron_preamble_carries_rule_index(self):
        tenant = create_tenant(display_name="Cron Rule Index", telegram_chat_id=920101)
        with override_settings(SUBAGENT_TENANT_IDS=str(tenant.id)):
            cron_prompt = _prepare_cron_prompt("Task body", tenant)

        self.assertIn("On-demand rule files (you can read these in this session)", cron_prompt)
        self.assertIn(
            "Call `nbhd_journal_context` only when the task below needs recent journal/backbone context; "
            "otherwise skip it.",
            cron_prompt,
        )
        for path, load_for in _CRON_INDEX_ROWS:
            with self.subTest(path=path):
                self.assertIn(f"| `{path}` | {load_for} |", cron_prompt)
        self.assertIn(
            "| `rules/subagents.md` | Slow-task delegation and app completion delivery |",
            cron_prompt,
        )
        self.assertNotIn("| `rules/memory.md` |", cron_prompt)
        self.assertNotIn("| `rules/document-ingestion.md` |", cron_prompt)
        self.assertNotIn("| `rules/reply-markers.md` |", cron_prompt)
        self.assertNotIn("| `rules/onboarding.md` |", cron_prompt)

    def test_all_gates_render_within_pin(self):
        prompt = render_workspace_files("neighbor", tenant=self._all_gates_tenant())["NBHD_AGENTS_MD"]

        # Measured after rules-delivery W0: 21,920 chars (R0 ceiling 22,759). 2026-08-30: +1,665 for the per-tenant "## Website edit gate" (site_editor_enabled) → 23,585; runtime cap BOOTSTRAP_MAX_CHARS=26,000.
        self.assertLessEqual(len(prompt), 23_585)


class ToolsReferenceCronStubTest(SimpleTestCase):
    def test_stub_is_small_cron_only_and_keeps_only_cross_tool_contracts(self):
        content = _TOOLS_REFERENCE.read_text()

        self.assertTrue(content.startswith(_TOOLS_REFERENCE_HEADER))
        self.assertLessEqual(len(content), 2_500)
        self.assertIn("last line, or immediately before a final `[[quick-replies: ...]]` line", content)
        self.assertIn("**Session-start check:**", content)
        self.assertIn("the user has explicitly approved it", content)
        self.assertIn(
            "For Fuel plans/fill-ins, first use `tool_search` for exact `nbhd_fuel_search_exercises` and "
            "call it per accessory/mobility group; then find/call `nbhd_fuel_create_plan`/"
            "`nbhd_fuel_update_plan`. Plans four weeks or longer rotate accessories every 1–2 weeks.",
            content,
        )
        self.assertNotIn("| Tool |", content)
