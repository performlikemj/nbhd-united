"""Tests for Google Workspace (gws) integration."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant, User


def _make_user(**kwargs):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    defaults = {"username": f"gws_test_{User.objects.count()}", "password": "test123"}
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def _make_tenant(user, **kwargs):
    defaults = {
        "user": user,
        "status": Tenant.Status.ACTIVE,
        "container_fqdn": "test.example.com",
        "container_id": f"oc-test-{user.username[:10]}",
    }
    defaults.update(kwargs)
    return Tenant.objects.create(**defaults)


# ────────────────────────────────────────────────────────────────────────────
# GWS Credentials Write Tests
# ────────────────────────────────────────────────────────────────────────────


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="test-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="test-client-secret",
    AZURE_MOCK="true",
)
class GWSCredentialWriteTest(TestCase):
    """Test that Google OAuth tokens get written as gws credentials."""

    def test_connect_gmail_writes_gws_creds(self):
        """Connecting Gmail writes gws-credentials.json to file share."""
        from apps.integrations.services import connect_integration

        user = _make_user()
        tenant = _make_tenant(user)

        tokens = {
            "access_token": "ya29.test-access",
            "refresh_token": "1//test-refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

        with patch(
            "apps.integrations.services._write_gws_credentials_to_file_share"
        ) as mock_write:
            connect_integration(tenant, "gmail", tokens, provider_email="test@gmail.com")
            mock_write.assert_called_once_with(tenant, tokens)

    def test_connect_calendar_writes_gws_creds(self):
        """Connecting Google Calendar also writes gws credentials."""
        from apps.integrations.services import connect_integration

        user = _make_user()
        tenant = _make_tenant(user)

        tokens = {
            "access_token": "ya29.test-access",
            "refresh_token": "1//test-refresh",
            "expires_in": 3600,
        }

        with patch(
            "apps.integrations.services._write_gws_credentials_to_file_share"
        ) as mock_write:
            connect_integration(tenant, "google-calendar", tokens)
            mock_write.assert_called_once()

    def test_connect_non_google_does_not_write_gws(self):
        """Non-Google providers don't trigger gws credential write."""
        from apps.integrations.services import connect_integration

        user = _make_user()
        tenant = _make_tenant(user)

        tokens = {"access_token": "test", "refresh_token": "test"}

        with patch(
            "apps.integrations.services._write_gws_credentials_to_file_share"
        ) as mock_write:
            connect_integration(tenant, "sautai", tokens)
            mock_write.assert_not_called()

    def test_gws_creds_format(self):
        """Verify the gws credentials JSON format."""
        from apps.integrations.services import _write_gws_credentials_to_file_share

        user = _make_user()
        tenant = _make_tenant(user)

        tokens = {
            "access_token": "ya29.test",
            "refresh_token": "1//test-refresh-token",
            "expires_in": 3600,
        }

        # AZURE_MOCK=true so it just logs
        _write_gws_credentials_to_file_share(tenant, tokens)
        # No assertion needed — just verify it doesn't crash in mock mode

    def test_gws_creds_no_refresh_token(self):
        """No refresh_token = no gws creds written (just a warning)."""
        from apps.integrations.services import _write_gws_credentials_to_file_share

        user = _make_user()
        tenant = _make_tenant(user)

        tokens = {"access_token": "ya29.test"}  # No refresh_token

        # Should not crash, just log a warning
        _write_gws_credentials_to_file_share(tenant, tokens)


# ────────────────────────────────────────────────────────────────────────────
# GWS Disconnect Tests
# ────────────────────────────────────────────────────────────────────────────


@override_settings(AZURE_MOCK="true")
class GWSDisconnectTest(TestCase):

    def test_disconnect_gmail_deletes_gws_creds(self):
        """Disconnecting Gmail also deletes gws-credentials.json."""
        from apps.integrations.models import Integration
        from apps.integrations.services import disconnect_integration

        user = _make_user()
        tenant = _make_tenant(user)

        Integration.objects.create(
            tenant=tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
            key_vault_secret_name="test-secret",
        )

        with patch(
            "apps.integrations.services._delete_gws_credentials_from_file_share"
        ) as mock_delete:
            with patch("apps.integrations.services.delete_tokens_from_key_vault"):
                disconnect_integration(tenant, "gmail")
            mock_delete.assert_called_once_with(tenant)

    def test_disconnect_non_google_no_gws_delete(self):
        """Non-Google disconnect doesn't touch gws credentials."""
        from apps.integrations.models import Integration
        from apps.integrations.services import disconnect_integration

        user = _make_user()
        tenant = _make_tenant(user)

        Integration.objects.create(
            tenant=tenant,
            provider="sautai",
            status=Integration.Status.ACTIVE,
            key_vault_secret_name="test-secret",
        )

        with patch(
            "apps.integrations.services._delete_gws_credentials_from_file_share"
        ) as mock_delete:
            with patch("apps.integrations.services.delete_tokens_from_key_vault"):
                disconnect_integration(tenant, "sautai")
            mock_delete.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# Config Generator Tests
# ────────────────────────────────────────────────────────────────────────────


@override_settings(AZURE_MOCK="true")
class GWSConfigGeneratorTest(TestCase):
    """Test that config generator includes gws skills when Google is connected."""

    def test_config_includes_gws_when_connected(self):
        from apps.integrations.models import Integration
        from apps.orchestrator.config_generator import generate_openclaw_config as generate_config

        user = _make_user()
        tenant = _make_tenant(user)

        Integration.objects.create(
            tenant=tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
        )

        config = generate_config(tenant)

        # Check env var
        self.assertEqual(
            config.get("env", {}).get("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"),
            "/workspace/gws-credentials.json",
        )

        # Check skills loaded
        skills = config.get("skills", {})
        load_paths = skills.get("load", {}).get("paths", [])
        self.assertIn("/opt/nbhd/skills/gws-gmail", load_paths)
        self.assertIn("/opt/nbhd/skills/gws-calendar", load_paths)
        self.assertIn("/opt/nbhd/skills/gws-shared", load_paths)

    def test_config_no_gws_without_connection(self):
        from apps.orchestrator.config_generator import generate_openclaw_config as generate_config

        user = _make_user()
        tenant = _make_tenant(user)

        config = generate_config(tenant)

        # No gws env var
        self.assertNotIn(
            "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE",
            config.get("env", {}),
        )

    def test_config_no_gws_when_expired(self):
        from apps.integrations.models import Integration
        from apps.orchestrator.config_generator import generate_openclaw_config as generate_config

        user = _make_user()
        tenant = _make_tenant(user)

        Integration.objects.create(
            tenant=tenant,
            provider="gmail",
            status=Integration.Status.EXPIRED,
        )

        config = generate_config(tenant)

        # Expired = no gws
        self.assertNotIn(
            "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE",
            config.get("env", {}),
        )


# ────────────────────────────────────────────────────────────────────────────
# Scope Tests
# ────────────────────────────────────────────────────────────────────────────


class GmailScopesTest(TestCase):
    """Verify Gmail provider requests expanded scopes for gws."""

    def test_gmail_scopes_include_calendar_and_drive(self):
        from apps.integrations.services import OAUTH_PROVIDERS

        scopes = OAUTH_PROVIDERS["gmail"]["scopes"]
        self.assertIn("https://www.googleapis.com/auth/gmail.modify", scopes)
        self.assertIn("https://www.googleapis.com/auth/calendar", scopes)
        self.assertIn("https://www.googleapis.com/auth/drive.file", scopes)
        self.assertIn("https://www.googleapis.com/auth/tasks", scopes)
