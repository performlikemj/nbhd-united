"""Packaging, schema, and B2b emission guards for Calendar & Reminders."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.config_security import audit_config_security
from apps.orchestrator.config_validator import validate_openclaw_config
from apps.tenants.services import create_tenant

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "runtime/openclaw/plugins/nbhd-datebook-tools"
_PLUGIN_ID = "nbhd-datebook-tools"
_TOOLS = {
    "nbhd_datebook_read",
    "nbhd_datebook_add_event",
    "nbhd_datebook_add_apple_reminder",
}


class DatebookToolSchemaTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = (_PLUGIN_DIR / "index.js").read_text()
        cls.manifest = json.loads((_PLUGIN_DIR / "openclaw.plugin.json").read_text())

    def test_manifest_is_strict_and_contracts_all_three_tools(self):
        self.assertFalse(self.manifest["configSchema"]["additionalProperties"])
        self.assertEqual(set(self.manifest["contracts"]["tools"]), _TOOLS)

    def test_tool_schemas_and_descriptions_pin_the_safety_contract(self):
        for tool in _TOOLS:
            self.assertIn(f'name: "{tool}"', self.source)
        self.assertGreaterEqual(self.source.count("additionalProperties: false"), 14)
        self.assertIn("nbhd_cron_create_pure_reminder", self.source)
        self.assertIn("attendees", self.source)
        self.assertIn("alarm only when", self.source)
        self.assertIn("queued for up to 72 hours", self.source)
        self.assertIn("Mirror/list state may be stale", self.source)

    def test_runtime_transport_and_untrusted_content_boundary_are_present(self):
        self.assertIn('import { wrapTool } from "../../tool-logger.js"', self.source)
        self.assertIn('import { wrapExternalContent } from "../../external-content-wrap.js"', self.source)
        self.assertIn('"X-NBHD-Internal-Key"', self.source)
        self.assertIn('"X-NBHD-Tenant-Id"', self.source)
        self.assertIn("DEFAULT_REQUEST_TIMEOUT_MS = 20000", self.source)
        self.assertIn("MAX_POLL_MS = 10000", self.source)
        self.assertIn("wrapExternalContent(JSON.stringify(payload.items", self.source)
        self.assertIn("synced ${ageHours}h ago", self.source)


class DatebookPluginEmissionTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Datebook Emission", telegram_chat_id=760011)

    def _set_readiness(self, *, manifest_ok: bool, enabled: bool, events: bool, reminders: bool) -> None:
        now = timezone.now()
        self.tenant.datebook_manifest_ok = manifest_ok
        self.tenant.datebook_enabled = enabled
        self.tenant.datebook_events_consent_at = now if events else None
        self.tenant.datebook_reminders_consent_at = now if reminders else None

    def _assert_emission(self, *, emitted: bool) -> dict:
        config = generate_openclaw_config(self.tenant)
        plugins = config.get("plugins", {})
        assertion = self.assertIn if emitted else self.assertNotIn
        assertion(_PLUGIN_ID, plugins.get("allow", []))
        assertion(_PLUGIN_ID, plugins.get("entries", {}))
        assertion(
            f"/opt/nbhd/plugins/{_PLUGIN_ID}",
            plugins.get("load", {}).get("paths", []),
        )
        return config

    def test_plugin_emission_matches_eight_combination_readiness_matrix(self):
        for manifest_ok, enabled, consent in itertools.product((False, True), repeat=3):
            with self.subTest(
                manifest_ok=manifest_ok,
                enabled=enabled,
                consent=consent,
            ):
                self._set_readiness(
                    manifest_ok=manifest_ok,
                    enabled=enabled,
                    events=consent,
                    reminders=False,
                )
                self._assert_emission(emitted=manifest_ok and enabled and consent)

    def test_either_consent_scope_is_sufficient(self):
        for events, reminders in ((True, False), (False, True), (True, True)):
            with self.subTest(events=events, reminders=reminders):
                self._set_readiness(
                    manifest_ok=True,
                    enabled=True,
                    events=events,
                    reminders=reminders,
                )
                self._assert_emission(emitted=True)

    @override_settings(OPENCLAW_DATEBOOK_PLUGIN_ID="")
    def test_empty_plugin_id_smoke_disables_ready_tenant(self):
        self._set_readiness(manifest_ok=True, enabled=True, events=True, reminders=False)
        self._assert_emission(emitted=False)

    def test_ready_plugin_wiring_passes_validator_and_security_consistency(self):
        self._set_readiness(manifest_ok=True, enabled=True, events=False, reminders=True)
        config = self._assert_emission(emitted=True)

        plugins = config["plugins"]
        self.assertEqual(plugins["entries"][_PLUGIN_ID], {"enabled": True})
        self.assertIn("group:plugins", config["tools"]["allow"])

        validator_errors = [
            issue for issue in validate_openclaw_config(config, strict=True) if issue.severity == "error"
        ]
        self.assertEqual(validator_errors, [])
        plugin_findings = [finding for finding in audit_config_security(config) if finding.check == "plugin_orphans"]
        self.assertEqual(plugin_findings, [])
