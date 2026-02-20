"""Tests for the BYOK model fetching endpoint and provider_models module."""
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.crypto import encrypt_api_key
from apps.tenants.models import Tenant, UserLLMConfig
from apps.tenants.provider_models import fetch_models
from apps.tenants.services import create_tenant


class FetchModelsUnitTest(TestCase):
    """Unit tests for provider_models.fetch_models()."""

    @patch("apps.tenants.provider_models.requests.get")
    def test_openai_filters_chat_models(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"id": "gpt-5.2"},
                {"id": "gpt-4.1"},
                {"id": "text-embedding-3-large"},
                {"id": "dall-e-3"},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        models = fetch_models("openai", "sk-test")
        ids = [m["id"] for m in models]
        assert "openai/gpt-5.2" in ids
        assert "openai/gpt-4.1" in ids
        assert "openai/text-embedding-3-large" not in ids
        assert "openai/dall-e-3" not in ids

    @patch("apps.tenants.provider_models.requests.get")
    def test_anthropic_models(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"id": "claude-sonnet-4-20250514", "display_name": "Claude Sonnet 4", "context_window": 200000},
                {"id": "claude-opus-4-6", "display_name": "Claude Opus 4", "context_window": 200000},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        models = fetch_models("anthropic", "sk-ant-test")
        assert len(models) == 2
        assert models[0]["id"] == "anthropic/claude-opus-4-6"
        assert models[0]["context_window"] == 200000

    @patch("apps.tenants.provider_models.requests.get")
    def test_google_filters_generate_content(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "models": [
                {"name": "models/gemini-3-pro", "displayName": "Gemini 3 Pro",
                 "supportedGenerationMethods": ["generateContent"], "inputTokenLimit": 1000000},
                {"name": "models/embedding-001", "displayName": "Embedding",
                 "supportedGenerationMethods": ["embedContent"]},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        models = fetch_models("google", "AIza-test")
        assert len(models) == 1
        assert models[0]["id"] == "google/gemini-3-pro"

    def test_unsupported_provider_raises(self):
        with self.assertRaises(ValueError):
            fetch_models("unsupported", "key")

    @patch("apps.tenants.provider_models.requests.get")
    def test_groq_models(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"id": "llama-4-scout", "context_window": 131072},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        models = fetch_models("groq", "gsk-test")
        assert models[0]["id"] == "groq/llama-4-scout"
        assert models[0]["context_window"] == 131072


class FetchModelsViewTest(TestCase):
    """Integration tests for the FetchModelsView API endpoint."""

    def setUp(self):
        self.tenant = create_tenant(display_name="BYOK Tester", telegram_chat_id=99999)
        self.tenant.model_tier = "byok"
        self.tenant.status = "active"
        self.tenant.save()
        self.user = self.tenant.user
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.tenants.llm_config_views.fetch_models")
    def test_fetch_models_with_provided_key(self, mock_fetch):
        mock_fetch.return_value = [
            {"id": "openai/gpt-5.2", "name": "gpt-5.2", "context_window": None}
        ]
        resp = self.client.post(
            "/api/v1/tenants/settings/llm-config/models/",
            {"provider": "openai", "api_key": "sk-test"},
            format="json",
        )
        assert resp.status_code == 200
        assert len(resp.json()["models"]) == 1
        mock_fetch.assert_called_once_with("openai", "sk-test")

    @patch("apps.tenants.llm_config_views.fetch_models")
    def test_fetch_models_falls_back_to_stored_key(self, mock_fetch):
        mock_fetch.return_value = []
        UserLLMConfig.objects.create(
            user=self.user,
            provider="anthropic",
            encrypted_api_key=encrypt_api_key("sk-stored-key"),
        )
        resp = self.client.post(
            "/api/v1/tenants/settings/llm-config/models/",
            {"provider": "anthropic"},
            format="json",
        )
        assert resp.status_code == 200
        mock_fetch.assert_called_once_with("anthropic", "sk-stored-key")

    def test_non_byok_user_forbidden(self):
        self.tenant.model_tier = "starter"
        self.tenant.save()
        resp = self.client.post(
            "/api/v1/tenants/settings/llm-config/models/",
            {"provider": "openai", "api_key": "sk-test"},
            format="json",
        )
        assert resp.status_code == 403

    def test_missing_provider(self):
        resp = self.client.post(
            "/api/v1/tenants/settings/llm-config/models/",
            {"api_key": "sk-test"},
            format="json",
        )
        assert resp.status_code == 400

    def test_no_key_anywhere(self):
        resp = self.client.post(
            "/api/v1/tenants/settings/llm-config/models/",
            {"provider": "openai"},
            format="json",
        )
        assert resp.status_code == 400
        assert "No API key" in resp.json()["error"]

    @patch("apps.tenants.llm_config_views.fetch_models")
    def test_invalid_key_returns_401(self, mock_fetch):
        from requests import HTTPError, Response as ReqResponse
        mock_resp = ReqResponse()
        mock_resp.status_code = 401
        mock_fetch.side_effect = HTTPError(response=mock_resp)
        resp = self.client.post(
            "/api/v1/tenants/settings/llm-config/models/",
            {"provider": "openai", "api_key": "sk-bad"},
            format="json",
        )
        assert resp.status_code == 401
        assert "Invalid API key" in resp.json()["error"]
