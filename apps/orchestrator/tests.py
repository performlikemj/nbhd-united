"""Tests for orchestrator app."""
import os
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from .config_generator import generate_openclaw_config
from .services import provision_tenant, deprovision_tenant, update_tenant_config


class ConfigGeneratorTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Config Test",
            telegram_chat_id=999888777,
        )

    def test_generates_valid_config(self):
        config = generate_openclaw_config(self.tenant)
        self.assertIn("gateway", config)
        self.assertIn("channels", config)
        self.assertIn("agents", config)
        self.assertEqual(config["gateway"]["mode"], "local")

    def test_gateway_defaults_use_supported_bind_mode(self):
        config = generate_openclaw_config(self.tenant)
        self.assertEqual(config["gateway"]["bind"], "loopback")
        # Auth is intentionally present — token from env var for Django→OC calls
        self.assertEqual(config["gateway"]["auth"]["mode"], "token")

    def test_chat_id_in_allow_from(self):
        config = generate_openclaw_config(self.tenant)
        allow_from = config["channels"]["telegram"]["allowFrom"]
        self.assertIn("999888777", allow_from)

    def test_starter_tier_model(self):
        self.tenant.model_tier = "starter"
        config = generate_openclaw_config(self.tenant)
        self.assertIn("kimi", config["agents"]["defaults"]["model"]["primary"].lower())

    def test_starter_tier_uses_openrouter(self):
        self.tenant.model_tier = "starter"
        config = generate_openclaw_config(self.tenant)
        primary = config["agents"]["defaults"]["model"]["primary"]
        self.assertTrue(primary.startswith("openrouter/"))
        # OpenRouter is built-in; no custom providers block needed
        self.assertNotIn("models", config)

    def test_premium_tier_has_opus(self):
        self.tenant.model_tier = "premium"
        config = generate_openclaw_config(self.tenant)
        models = config["agents"]["defaults"]["models"]
        aliases = [v.get("alias") for v in models.values()]
        self.assertIn("opus", aliases)

    def test_byok_tier_generates_config(self):
        self.tenant.model_tier = "byok"
        config = generate_openclaw_config(self.tenant)
        self.assertIn("sonnet", config["agents"]["defaults"]["model"]["primary"].lower())
        # byok should not have extra models block
        self.assertNotIn("models", config)

    def test_audio_model_defaults_to_whisper_for_all_tiers(self):
        for tier in ("starter", "premium", "byok"):
            self.tenant.model_tier = tier
            config = generate_openclaw_config(self.tenant)
            audio = config["tools"]["media"]["audio"]
            self.assertTrue(audio["enabled"])
            models = audio["models"]
            self.assertEqual(len(models), 1)
            self.assertEqual(
                models[0],
                {"provider": "openai", "model": "gpt-4o-mini-transcribe"},
            )

    def test_plugin_wiring_enabled_when_plugin_id_configured(self):
        with override_settings(
            OPENCLAW_GOOGLE_PLUGIN_ID="nbhd-google-tools",
            OPENCLAW_JOURNAL_PLUGIN_ID="nbhd-journal-tools",
        ):
            config = generate_openclaw_config(self.tenant)

        self.assertEqual(
            sorted(config["plugins"]["allow"]),
            ["nbhd-google-tools", "nbhd-journal-tools"],
        )
        self.assertTrue(config["plugins"]["entries"]["nbhd-google-tools"]["enabled"])
        self.assertTrue(config["plugins"]["entries"]["nbhd-journal-tools"]["enabled"])
        paths = config["plugins"]["load"]["paths"]
        self.assertIn("/opt/nbhd/plugins/nbhd-google-tools", paths)
        self.assertIn("/opt/nbhd/plugins/nbhd-journal-tools", paths)
        self.assertIn("group:plugins", config["tools"]["allow"])

    def test_plugin_wiring_omitted_when_no_plugins_configured(self):
        with override_settings(OPENCLAW_GOOGLE_PLUGIN_ID="", OPENCLAW_JOURNAL_PLUGIN_ID=""):
            config = generate_openclaw_config(self.tenant)

        self.assertNotIn("plugins", config)
        # group:plugins is in the base tool policy (tool_policy.py), not added by plugin wiring
        self.assertIn("group:plugins", config["tools"]["allow"])

    def test_single_plugin_wired_when_only_one_configured(self):
        with override_settings(
            OPENCLAW_GOOGLE_PLUGIN_ID="nbhd-google-tools",
            OPENCLAW_JOURNAL_PLUGIN_ID="",
        ):
            config = generate_openclaw_config(self.tenant)

        self.assertEqual(config["plugins"]["allow"], ["nbhd-google-tools"])
        self.assertNotIn("nbhd-journal-tools", config["plugins"]["entries"])

    def test_tools_policy_uses_allow_and_deny_lists(self):
        self.tenant.model_tier = "starter"
        config = generate_openclaw_config(self.tenant)
        tools = config["tools"]
        self.assertIn("allow", tools)
        self.assertIn("deny", tools)
        self.assertIn("gateway", tools["deny"])
        self.assertNotIn("group:automation", tools["deny"])
        self.assertNotIn("group:ui", tools["allow"])

    def test_premium_tier_tools_enable_browser_and_exec(self):
        self.tenant.model_tier = "premium"
        config = generate_openclaw_config(self.tenant)
        tools = config["tools"]
        self.assertIn("group:ui", tools["allow"])
        self.assertIn("group:runtime", tools["allow"])
        self.assertEqual(tools["elevated"], {"enabled": False})

    def test_polling_mode_no_webhook_fields(self):
        """Polling mode: no webhookUrl/webhookHost/webhookSecret in config."""
        config = generate_openclaw_config(self.tenant)
        tg = config["channels"]["telegram"]
        self.assertNotIn("webhookUrl", tg)
        self.assertNotIn("webhookHost", tg)
        self.assertNotIn("webhookSecret", tg)

    def test_network_auto_select_family_disabled(self):
        """IPv6 autoSelectFamily disabled to prevent Azure Container Apps issues."""
        config = generate_openclaw_config(self.tenant)
        tg = config["channels"]["telegram"]
        self.assertFalse(tg["network"]["autoSelectFamily"])

    def test_config_with_no_chat_id_uses_disabled_dm_policy(self):
        self.tenant.user.telegram_chat_id = None
        self.tenant.user.save(update_fields=["telegram_chat_id"])
        config = generate_openclaw_config(self.tenant)
        tg = config["channels"]["telegram"]
        self.assertEqual(tg["dmPolicy"], "disabled")
        self.assertNotIn("allowFrom", tg)


@override_settings()
class ProvisioningTest(TestCase):
    def setUp(self):
        os.environ["AZURE_MOCK"] = "true"
        self.tenant = create_tenant(
            display_name="Provision Test",
            telegram_chat_id=111222333,
        )

    def tearDown(self):
        os.environ.pop("AZURE_MOCK", None)

    def test_provision_creates_container(self):
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        self.assertTrue(self.tenant.container_id.startswith("oc-"))
        self.assertTrue(self.tenant.container_fqdn)

    def test_deprovision_marks_deleted(self):
        provision_tenant(str(self.tenant.id))
        deprovision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.DELETED)
        self.assertEqual(self.tenant.container_id, "")

    @patch("apps.orchestrator.services.upload_config_to_file_share")
    def test_update_tenant_config_pushes_new_config(self, mock_upload):
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        mock_upload.reset_mock()
        update_tenant_config(str(self.tenant.id))

        # File share is updated (source of truth for OpenClaw)
        mock_upload.assert_called_once()
        upload_args = mock_upload.call_args[0]
        self.assertEqual(upload_args[0], str(self.tenant.id))
        self.assertIn("111222333", upload_args[1])
