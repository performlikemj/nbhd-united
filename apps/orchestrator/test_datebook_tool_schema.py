"""B2a packaging/schema guards for the dormant Calendar & Reminders plugin."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.orchestrator.config_generator import generate_openclaw_config
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


class DatebookPluginDormancyTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Datebook Dormant", telegram_chat_id=760011)

    def test_plugin_is_never_emitted_for_any_b2a_readiness_combination(self):
        now = timezone.now()
        for manifest_ok, enabled, event_consent, reminder_consent in itertools.product((False, True), repeat=4):
            with self.subTest(
                manifest_ok=manifest_ok,
                enabled=enabled,
                event_consent=event_consent,
                reminder_consent=reminder_consent,
            ):
                self.tenant.datebook_manifest_ok = manifest_ok
                self.tenant.datebook_enabled = enabled
                self.tenant.datebook_events_consent_at = now if event_consent else None
                self.tenant.datebook_reminders_consent_at = now if reminder_consent else None
                config = generate_openclaw_config(self.tenant)
                plugins = config.get("plugins", {})
                self.assertNotIn(_PLUGIN_ID, plugins.get("allow", []))
                self.assertNotIn(_PLUGIN_ID, plugins.get("entries", {}))
                self.assertNotIn(
                    f"/opt/nbhd/plugins/{_PLUGIN_ID}",
                    plugins.get("load", {}).get("paths", []),
                )
