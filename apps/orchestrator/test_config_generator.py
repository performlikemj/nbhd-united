"""Tests for OpenClaw config generation."""
from __future__ import annotations

from unittest.mock import MagicMock

from django.test import SimpleTestCase, override_settings

from apps.orchestrator.config_generator import generate_openclaw_config


def _make_tenant(tier="basic", chat_id=None, timezone="UTC", display_name="Test"):
    user = MagicMock()
    user.telegram_chat_id = chat_id
    user.timezone = timezone
    user.display_name = display_name

    tenant = MagicMock()
    tenant.user = user
    tenant.model_tier = tier
    return tenant


@override_settings(
    OPENCLAW_GOOGLE_PLUGIN_ID="",
    OPENCLAW_GOOGLE_PLUGIN_PATH="",
    API_BASE_URL="https://api.example.com",
    TELEGRAM_WEBHOOK_SECRET="secret",
)
class ConfigGeneratorTest(SimpleTestCase):
    def test_config_includes_whisper_skill(self):
        tenant = _make_tenant()
        config = generate_openclaw_config(tenant)

        self.assertIn("skills", config)
        self.assertIn("openai-whisper-api", config["skills"]["entries"])
        self.assertTrue(config["skills"]["entries"]["openai-whisper-api"]["enabled"])

    def test_config_includes_web_search_enabled(self):
        tenant = _make_tenant()
        config = generate_openclaw_config(tenant)

        self.assertTrue(config["tools"]["web"]["search"]["enabled"])

    def test_basic_tier_tools_include_network(self):
        tenant = _make_tenant(tier="basic")
        config = generate_openclaw_config(tenant)

        self.assertIn("group:network", config["tools"]["allow"])

    def test_plus_tier_tools_include_browser(self):
        tenant = _make_tenant(tier="plus")
        config = generate_openclaw_config(tenant)

        self.assertIn("group:browser", config["tools"]["allow"])
