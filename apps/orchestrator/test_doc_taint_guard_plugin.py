"""Tests for the nbhd-doc-taint-guard plugin gating in generated openclaw.json.

Unlike nbhd-stream-progress (opt-in, ID defaults to "") or nbhd-document-keep
(fail-closed on the per-tenant ``document_ingestion_enabled`` flag), the doc
taint guard must load UNCONDITIONALLY — the built-in ``pdf``/``image`` tools
are fleet-wide (apps.orchestrator.tool_policy), so the guard protecting them
has to be too, for every tenant regardless of any per-tenant flag. This locks
in "on by default" and the ``mode`` (log_only/enforce) wiring from
docs/upload-security-threat-model.md P0-2.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from apps.orchestrator.config_generator import generate_openclaw_config
from apps.tenants.services import create_tenant

PLUGIN_ID = "nbhd-doc-taint-guard"


class DocTaintGuardPluginGatingTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="TaintGuard", telegram_chat_id=750002)

    def test_included_unconditionally_by_default(self):
        # No special tenant flags set (in particular, document_ingestion_enabled
        # is untouched / defaults False) — the guard must still load.
        config = generate_openclaw_config(self.tenant)
        plugins = config.get("plugins", {})
        self.assertIn(PLUGIN_ID, plugins.get("entries", {}))
        self.assertIn(PLUGIN_ID, plugins.get("allow", []))
        self.assertIn(PLUGIN_ID, " ".join(plugins.get("load", {}).get("paths", [])))

    def test_default_mode_is_log_only(self):
        config = generate_openclaw_config(self.tenant)
        entry = config["plugins"]["entries"][PLUGIN_ID]
        self.assertEqual(entry.get("config"), {"mode": "log_only"})

    @override_settings(DOC_TAINT_GATE_MODE="enforce")
    def test_mode_flips_to_enforce_via_single_settings_override(self):
        # Fleet-wide flip is one settings/env var, no per-tenant migration.
        config = generate_openclaw_config(self.tenant)
        entry = config["plugins"]["entries"][PLUGIN_ID]
        self.assertEqual(entry.get("config"), {"mode": "enforce"})

    @override_settings(OPENCLAW_DOC_TAINT_GUARD_PLUGIN_ID="")
    def test_smoke_disable_via_empty_id_omits_the_plugin(self):
        # Mirrors the CI smoke script's disable mechanism for every other
        # unconditional plugin (routing-context, cron-enforcement, ...).
        config = generate_openclaw_config(self.tenant)
        plugins = config.get("plugins", {})
        self.assertNotIn(PLUGIN_ID, plugins.get("entries", {}))
        self.assertNotIn(PLUGIN_ID, plugins.get("allow", []))

    @override_settings(DOC_TAINT_GATE_MODE="Enforce")
    def test_garbage_mode_falls_back_to_log_only(self):
        # The plugin's manifest declares mode as enum:["log_only","enforce"]
        # with additionalProperties:false — OpenClaw validates each enabled
        # plugin's config against its schema at config LOAD time, so a
        # typo'd DOC_TAINT_GATE_MODE would make every regenerated tenant
        # config invalid fleet-wide (a #917-class wedge), not just this one
        # plugin misbehaving. The generator must normalize instead of
        # emitting the raw value verbatim.
        config = generate_openclaw_config(self.tenant)
        entry = config["plugins"]["entries"][PLUGIN_ID]
        self.assertEqual(entry.get("config"), {"mode": "log_only"})

    @override_settings(DOC_TAINT_GATE_MODE="")
    def test_empty_mode_falls_back_to_log_only(self):
        config = generate_openclaw_config(self.tenant)
        entry = config["plugins"]["entries"][PLUGIN_ID]
        self.assertEqual(entry.get("config"), {"mode": "log_only"})
