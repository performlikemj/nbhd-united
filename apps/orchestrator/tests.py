"""Tests for orchestrator app."""
import os
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from .config_generator import generate_openclaw_config
from .services import provision_tenant, deprovision_tenant


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

    def test_plugin_wiring_enabled_when_plugin_id_configured(self):
        with override_settings(OPENCLAW_GOOGLE_PLUGIN_ID="nbhd-google-tools"):
            config = generate_openclaw_config(self.tenant)

        self.assertEqual(config["plugins"]["allow"], ["nbhd-google-tools"])
        self.assertTrue(config["plugins"]["entries"]["nbhd-google-tools"]["enabled"])
        self.assertEqual(
            config["plugins"]["load"]["paths"],
            ["/opt/nbhd/plugins/nbhd-google-tools"],
        )
        self.assertIn("group:plugins", config["tools"]["alsoAllow"])

    def test_plugin_wiring_omitted_when_plugin_id_not_configured(self):
        with override_settings(OPENCLAW_GOOGLE_PLUGIN_ID=""):
            config = generate_openclaw_config(self.tenant)

        self.assertNotIn("plugins", config)
        self.assertNotIn("alsoAllow", config["tools"])


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
