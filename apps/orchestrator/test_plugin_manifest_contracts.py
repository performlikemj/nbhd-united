"""Repo-wide drift guards for declared OpenClaw tool contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.test import SimpleTestCase

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "runtime/openclaw/plugins"
_REGISTERED_TOOL = re.compile(
    r"api\.registerTool\s*\(\s*(?:wrap\s*\(\s*)?\{\s*name:\s*[\"']([^\"']+)[\"']",
    re.MULTILINE,
)


class PluginManifestContractTests(SimpleTestCase):
    def test_every_directly_registered_tool_is_declared_by_its_manifest(self):
        checked = 0
        for manifest_path in sorted(_PLUGIN_ROOT.glob("*/openclaw.plugin.json")):
            manifest = json.loads(manifest_path.read_text())
            contracted = manifest.get("contracts", {}).get("tools")
            index_path = manifest_path.with_name("index.js")
            if not isinstance(contracted, list) or not index_path.exists():
                continue

            checked += 1
            registered = set(_REGISTERED_TOOL.findall(index_path.read_text()))
            missing = registered - set(contracted)
            self.assertFalse(
                missing,
                f"{manifest_path.parent.name} directly registers undeclared tools: {sorted(missing)}",
            )

        self.assertGreater(checked, 0)

    def test_rules_delivery_query_tools_are_declared(self):
        expected = {
            "nbhd-journal-tools": "nbhd_journal_query",
            "nbhd-finance-tools": "nbhd_gravity_query",
            "nbhd-insights-tools": "nbhd_yesterdays_signals",
        }
        for plugin, tool in expected.items():
            manifest = json.loads((_PLUGIN_ROOT / plugin / "openclaw.plugin.json").read_text())
            with self.subTest(plugin=plugin):
                self.assertIn(tool, manifest["contracts"]["tools"])


class RulesDeliveryToolDescriptionTests(SimpleTestCase):
    def _description_section(self, plugin: str, tool: str) -> str:
        source = (_PLUGIN_ROOT / plugin / "index.js").read_text()
        start = source.index(f'name: "{tool}"')
        return source[start : source.index("parameters:", start)]

    def test_send_to_user_names_the_active_channel_not_telegram_only(self):
        description = self._description_section("nbhd-journal-tools", "nbhd_send_to_user")
        self.assertIn("active channel", description)
        self.assertIn("NBHD app", description)
        self.assertIn("Telegram", description)
        self.assertIn("LINE", description)
        self.assertNotIn("Telegram-only", description)

    def test_portfolio_title_fallback_is_in_the_tool_description(self):
        description = self._description_section("nbhd-site-publishing", "publish_portfolio_image")
        self.assertIn("generate a title from the image or ask once", description)
        self.assertIn("reuse the shared theme across all images", description)
