"""Tests for the nbhd-stream-progress plugin gating in generated openclaw.json.

The stream-progress plugin (per-step partial assistant text → the chat/progress
endpoint) is opt-in exactly like nbhd-activity-stream: its ID defaults to "" so
the config generator's ``if pid`` guard filters it out until an operator sets
``OPENCLAW_STREAM_PROGRESS_PLUGIN_ID``. This locks in "dormant by default,
included only when enabled".
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from apps.orchestrator.config_generator import generate_openclaw_config
from apps.tenants.services import create_tenant

PLUGIN_ID = "nbhd-stream-progress"


class StreamProgressPluginGatingTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="StreamProg", telegram_chat_id=750001)

    def test_omitted_when_id_unset(self):
        # Default (ID "") → filtered out by the `if pid` guard.
        config = generate_openclaw_config(self.tenant)
        plugins = config.get("plugins", {})
        self.assertNotIn(PLUGIN_ID, plugins.get("entries", {}))
        self.assertNotIn(PLUGIN_ID, plugins.get("allow", []))

    @override_settings(OPENCLAW_STREAM_PROGRESS_PLUGIN_ID=PLUGIN_ID)
    def test_included_when_id_set(self):
        config = generate_openclaw_config(self.tenant)
        plugins = config.get("plugins", {})
        self.assertIn(PLUGIN_ID, plugins.get("entries", {}))
        self.assertEqual(plugins["entries"][PLUGIN_ID], {"enabled": True})
        self.assertIn(PLUGIN_ID, plugins.get("allow", []))
