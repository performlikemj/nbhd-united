"""Tests for BYOK LLM config: crypto, API, and config generator integration."""
import json

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.crypto import decrypt_api_key, encrypt_api_key
from apps.tenants.models import Tenant, User, UserLLMConfig


@override_settings(SECRET_KEY="test-secret-key-for-fernet")
class CryptoTests(TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        original = "sk-abc123xyz"
        encrypted = encrypt_api_key(original)
        self.assertNotEqual(encrypted, original)
        self.assertEqual(decrypt_api_key(encrypted), original)

    def test_empty_string(self):
        self.assertEqual(encrypt_api_key(""), "")
        self.assertEqual(decrypt_api_key(""), "")


@override_settings(SECRET_KEY="test-secret-key-for-fernet")
class LLMConfigAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", password="testpass", display_name="Test"
        )
        self.tenant = Tenant.objects.create(user=self.user, model_tier="byok")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = "/api/v1/tenants/settings/llm-config/"

    def test_get_no_config(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["has_key"])

    def test_put_creates_config(self):
        resp = self.client.put(
            self.url,
            {"provider": "openai", "api_key": "sk-test123456789", "model_id": "openai/gpt-4o"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["has_key"])
        self.assertNotIn("sk-test123456789", json.dumps(resp.data))
        # Verify stored encrypted
        config = UserLLMConfig.objects.get(user=self.user)
        self.assertNotEqual(config.encrypted_api_key, "sk-test123456789")
        self.assertEqual(decrypt_api_key(config.encrypted_api_key), "sk-test123456789")

    def test_get_returns_masked_key(self):
        UserLLMConfig.objects.create(
            user=self.user,
            provider="anthropic",
            encrypted_api_key=encrypt_api_key("sk-ant-abcdefghijk"),
            model_id="anthropic/claude-sonnet-4-20250514",
        )
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["has_key"])
        self.assertIn("...", resp.data["key_masked"])
        self.assertNotEqual(resp.data["key_masked"], "sk-ant-abcdefghijk")


@override_settings(
    SECRET_KEY="test-secret-key-for-fernet",
    OPENCLAW_GOOGLE_PLUGIN_ID="",
    OPENCLAW_JOURNAL_PLUGIN_ID="",
)
class ConfigGeneratorBYOKTests(TestCase):
    def test_byok_injects_env_and_model(self):
        from apps.orchestrator.config_generator import generate_openclaw_config

        user = User.objects.create_user(
            username="byokuser", password="pass", display_name="BYOK User"
        )
        tenant = Tenant.objects.create(user=user, model_tier="byok", status="active")
        UserLLMConfig.objects.create(
            user=user,
            provider="openai",
            encrypted_api_key=encrypt_api_key("sk-my-key-123"),
            model_id="openai/gpt-4o",
        )

        config = generate_openclaw_config(tenant)
        self.assertEqual(config["env"]["OPENAI_API_KEY"], "sk-my-key-123")
        self.assertEqual(config["agents"]["defaults"]["model"]["primary"], "openai/gpt-4o")

    def test_byok_no_config_falls_back(self):
        from apps.orchestrator.config_generator import generate_openclaw_config

        user = User.objects.create_user(
            username="byokuser2", password="pass", display_name="BYOK User 2"
        )
        tenant = Tenant.objects.create(user=user, model_tier="byok", status="active")

        config = generate_openclaw_config(tenant)
        # Should not crash, no env key injected
        self.assertNotIn("env", config)
