"""AGENTS.md surface + config_generator gating for document information-keeping.

Three layers must stay separable (critic finding 5): the base behavioral gate and
the generic rules file are fleet-wide and tool-name-free; only a tenant with
document_ingestion_enabled sees the block that NAMES nbhd_document_* and only that
tenant loads the plugin. The base gate must also keep the finance-tenant AGENTS.md
under the per-file bootstrap budget (critic finding 11).
"""

from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings

from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.personas import render_workspace_files, render_workspace_rules
from apps.tenants.services import create_tenant

_TOOL_NAMES = ("nbhd_document_keep", "nbhd_document_forget", "nbhd_document_list_ingestions")
_BASE_GATE_PHRASES = ("about a day", "Never save on the same turn", "Answer first")


def _agents_md(tenant):
    return render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]


class BaseGateFleetWideTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Base", telegram_chat_id=900101)

    def test_base_gate_present_without_the_flag(self):
        md = _agents_md(self.tenant)
        for phrase in _BASE_GATE_PHRASES:
            self.assertIn(phrase, md)

    def test_base_gate_names_no_tool(self):
        md = _agents_md(self.tenant)
        for name in _TOOL_NAMES:
            self.assertNotIn(name, md)

    def test_generic_rules_file_is_fleet_wide(self):
        rules = render_workspace_rules()
        self.assertIn("document-ingestion.md", rules)
        body = rules["document-ingestion.md"]
        self.assertIn("never on the same turn the document arrived", body)
        # The generic fleet-wide rules must not name the removal/keep tools.
        self.assertNotIn("nbhd_document_keep", body)
        self.assertNotIn("nbhd_document_forget", body)


class FlagGatedToolBlockTest(TestCase):
    def setUp(self):
        self.plain = create_tenant(display_name="Plain", telegram_chat_id=900201)
        self.flagged = create_tenant(display_name="Flagged", telegram_chat_id=900202)
        self.flagged.document_ingestion_enabled = True
        self.flagged.save(update_fields=["document_ingestion_enabled"])

    def test_tool_block_only_for_flagged_tenant(self):
        flagged_md = _agents_md(self.flagged)
        for name in _TOOL_NAMES:
            self.assertIn(name, flagged_md)

    def test_non_flag_tenant_never_sees_a_tool_it_lacks(self):
        plain_md = _agents_md(self.plain)
        # Base gate yes, keep/list/forget tool language no. (The base body's
        # pre-existing nbhd_document_put/get/append journal tools are unrelated.)
        self.assertIn("about a day", plain_md)
        for name in _TOOL_NAMES:
            self.assertNotIn(name, plain_md)


# OpenClaw truncates each bootstrap file's TAIL beyond bootstrapMaxChars=18000. A
# finance tenant's AGENTS.md legitimately exceeds that (the ~6KB Gravity block is
# the intended truncation tail). Finding 11 is therefore about POSITION: the base
# gate + the flag-gated tool block must land ABOVE the cut, not that the total is
# under 18000 (it isn't, and wasn't before this change).
_BOOTSTRAP_MAX_CHARS = 18000


@override_settings(GRAVITY_ENABLED=True)
class FinanceTenantBudgetTest(TestCase):
    """critic finding 11 — the case the Gravity truncation logic exists for."""

    def test_load_bearing_blocks_survive_the_finance_truncation(self):
        tenant = create_tenant(display_name="Finance", telegram_chat_id=900301)
        tenant.finance_enabled = True
        tenant.document_ingestion_enabled = True
        tenant.save(update_fields=["finance_enabled", "document_ingestion_enabled"])
        md = _agents_md(tenant)
        self.assertTrue(tenant.finance_active)  # the truncation-relevant case is live

        gate_at = md.find("about a day")
        tool_at = md.find("nbhd_document_keep")
        gravity_at = md.find("Gravity Observation Mode")
        self.assertNotEqual(gate_at, -1)
        self.assertNotEqual(tool_at, -1)
        self.assertNotEqual(gravity_at, -1)

        # Base gate + tool block are above the truncation cut (survive bootstrap).
        self.assertLess(gate_at, _BOOTSTRAP_MAX_CHARS, "base gate falls in the truncated tail")
        self.assertLess(
            md.find("If you can't tell which document they mean"),  # end of the tool block
            _BOOTSTRAP_MAX_CHARS,
            "the flag-gated tool block falls in the truncated tail",
        )
        # The Gravity block is the intended tail — it sits AFTER the tool block.
        self.assertLess(tool_at, gravity_at)


class PluginEmissionTest(TestCase):
    def _plugins(self, tenant):
        config = generate_openclaw_config(tenant)
        plugins = config.get("plugins", {})
        paths = plugins.get("load", {}).get("paths", [])
        entries = plugins.get("entries", {})
        return paths, entries

    def test_plugin_not_emitted_without_flag(self):
        tenant = create_tenant(display_name="NoPlugin", telegram_chat_id=900401)
        paths, entries = self._plugins(tenant)
        self.assertNotIn("nbhd-document-keep", entries)
        self.assertNotIn("/opt/nbhd/plugins/nbhd-document-keep", paths)

    def test_plugin_emitted_with_config_when_flag_on(self):
        tenant = create_tenant(display_name="Plugin", telegram_chat_id=900402)
        tenant.document_ingestion_enabled = True
        tenant.save(update_fields=["document_ingestion_enabled"])
        paths, entries = self._plugins(tenant)
        self.assertIn("/opt/nbhd/plugins/nbhd-document-keep", paths)
        self.assertIn("nbhd-document-keep", entries)
        self.assertTrue(entries["nbhd-document-keep"]["config"]["documentIngestionEnabled"])
