"""Regression tests for fix cluster C26 (FA-0628).

Composio-managed providers (e.g. reddit) must not be run through the
OAuth client-credential refresh path. They are persisted ACTIVE with a
NULL token_expires_at and have no entry in PROVIDER_GROUP, so the old
refresh task flipped them to ERROR on every run.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.tenants.services import create_tenant

from .models import Integration
from .tasks import refresh_expiring_integrations_task


class ComposioProviderSkipTest(TestCase):
    def setUp(self):
        tenant = create_tenant(display_name="Reddit User", telegram_chat_id=626262)
        # Mirrors complete_composio_connection: ACTIVE, no token_expires_at.
        self.integration = Integration.objects.create(
            tenant=tenant,
            provider=Integration.Provider.REDDIT,
            status=Integration.Status.ACTIVE,
            token_expires_at=None,
        )

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_composio_reddit_integration_is_left_untouched(self, mock_load_tokens, mock_refresh):
        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        # Not selected by the query at all -> not checked, not errored.
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["errored"], 0)
        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(result["expired"], 0)
        # Still ACTIVE; Composio refreshes its own tokens.
        self.assertEqual(self.integration.status, Integration.Status.ACTIVE)
        mock_refresh.assert_not_called()
        mock_load_tokens.assert_not_called()
