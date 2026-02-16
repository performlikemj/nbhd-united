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
        self.assertEqual(config["gateway"]["bind"], "lan")
        self.assertNotIn("auth", config["gateway"])

    def test_chat_id_in_allow_from(self):
        config = generate_openclaw_config(self.tenant)
        allow_from = config["channels"]["telegram"]["allowFrom"]
        self.assertIn("999888777", allow_from)

    def test_basic_tier_model(self):
        self.tenant.model_tier = "basic"
        config = generate_openclaw_config(self.tenant)
        self.assertIn("sonnet", config["agents"]["defaults"]["model"]["primary"].lower())

    def test_plus_tier_has_opus(self):
        self.tenant.model_tier = "plus"
        config = generate_openclaw_config(self.tenant)
        models = config["agents"]["defaults"]["models"]
        aliases = [v.get("alias") for v in models.values()]
        self.assertIn("opus", aliases)

    def test_audio_model_defaults_to_whisper_for_all_tiers(self):
        for tier in ("basic", "plus"):
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
            config["plugins"]["allow"],
            ["nbhd-google-tools", "nbhd-journal-tools"],
        )
        self.assertTrue(config["plugins"]["entries"]["nbhd-google-tools"]["enabled"])
        self.assertTrue(config["plugins"]["entries"]["nbhd-journal-tools"]["enabled"])
        self.assertEqual(
            config["plugins"]["load"]["paths"],
            [
                "/opt/nbhd/plugins/nbhd-google-tools",
                "/opt/nbhd/plugins/nbhd-journal-tools",
            ],
        )
        self.assertIn("group:plugins", config["tools"]["alsoAllow"])

    def test_plugin_wiring_omitted_when_plugin_id_not_configured(self):
        with override_settings(OPENCLAW_GOOGLE_PLUGIN_ID=""):
            config = generate_openclaw_config(self.tenant)

        self.assertNotIn("plugins", config)
        self.assertNotIn("alsoAllow", config["tools"])

    def test_tools_policy_uses_allow_and_deny_lists(self):
        self.tenant.model_tier = "basic"
        config = generate_openclaw_config(self.tenant)
        tools = config["tools"]
        self.assertIn("allow", tools)
        self.assertIn("deny", tools)
        self.assertIn("group:automation", tools["deny"])
        self.assertNotIn("group:browser", tools["allow"])

    def test_plus_tier_tools_enable_browser_and_exec(self):
        self.tenant.model_tier = "plus"
        config = generate_openclaw_config(self.tenant)
        tools = config["tools"]
        self.assertIn("group:browser", tools["allow"])
        self.assertIn("exec", tools["allow"])
        self.assertEqual(tools["elevated"], {"enabled": False})

    @override_settings(
        API_BASE_URL="https://api.example.com",
        TELEGRAM_WEBHOOK_SECRET="runtime-webhook-secret",
    )
    def test_webhook_fields_set_when_settings_available(self):
        config = generate_openclaw_config(self.tenant)
        tg = config["channels"]["telegram"]
        self.assertEqual(tg["webhookUrl"], "https://api.example.com/api/v1/telegram/webhook/")
        self.assertEqual(tg["webhookHost"], "0.0.0.0")
        self.assertEqual(tg["webhookSecret"], "runtime-webhook-secret")

    @override_settings(API_BASE_URL="")
    def test_webhook_fields_omitted_when_settings_missing(self):
        config = generate_openclaw_config(self.tenant)
        tg = config["channels"]["telegram"]
        self.assertNotIn("webhookUrl", tg)
        self.assertNotIn("webhookSecret", tg)

    @override_settings(API_BASE_URL="https://api.example.com", TELEGRAM_WEBHOOK_SECRET="")
    def test_webhook_fields_omitted_when_webhook_secret_missing(self):
        config = generate_openclaw_config(self.tenant)
        tg = config["channels"]["telegram"]
        self.assertNotIn("webhookUrl", tg)
        self.assertNotIn("webhookSecret", tg)

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

    @patch("apps.orchestrator.services.update_container_env_var")
    @patch("apps.orchestrator.services.upload_config_to_file_share")
    def test_update_tenant_config_pushes_new_config(self, mock_upload, mock_update_env):
        provision_tenant(str(self.tenant.id))
        self.tenant.refresh_from_db()

        mock_upload.reset_mock()
        update_tenant_config(str(self.tenant.id))

        # File share is updated (source of truth for OpenClaw)
        mock_upload.assert_called_once()
        upload_args = mock_upload.call_args[0]
        self.assertEqual(upload_args[0], str(self.tenant.id))
        self.assertIn("111222333", upload_args[1])

        # Env var also updated for consistency (config + skill templates)
        self.assertGreaterEqual(mock_update_env.call_count, 1)
        config_call = mock_update_env.call_args_list[0]
        self.assertEqual(config_call[0][0], self.tenant.container_id)
        self.assertEqual(config_call[0][1], "OPENCLAW_CONFIG_JSON")
        self.assertIn("111222333", config_call[0][2])

        # Skill templates env var also pushed
        if mock_update_env.call_count >= 2:
            templates_call = mock_update_env.call_args_list[1]
            self.assertEqual(templates_call[0][1], "NBHD_SKILL_TEMPLATES_MD")
