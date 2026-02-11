"""Internal runtime integration endpoint tests."""
from __future__ import annotations

from unittest.mock import patch

import httpx
from django.test import TestCase
from django.test.utils import override_settings

from apps.tenants.services import create_tenant

from .services import (
    IntegrationNotConnectedError,
    IntegrationScopeError,
    ProviderAccessToken,
)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeIntegrationViewsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Runtime", telegram_chat_id=717171)
        self.other_tenant = create_tenant(display_name="Other Runtime", telegram_chat_id=727272)

    def _headers(self, tenant_id: str | None = None, key: str = "shared-key") -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def test_runtime_gmail_requires_internal_auth(self):
        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/gmail/messages/",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "internal_auth_failed")

    def test_runtime_gmail_rejects_tenant_scope_mismatch(self):
        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/gmail/messages/",
            **self._headers(tenant_id=str(self.other_tenant.id)),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "internal_auth_failed")

    @patch("apps.integrations.runtime_views.list_gmail_messages")
    @patch("apps.integrations.runtime_views.get_valid_provider_access_token")
    def test_runtime_gmail_returns_normalized_messages(self, mock_broker, mock_list_messages):
        mock_broker.return_value = ProviderAccessToken(
            access_token="access-token",
            expires_at=None,
            provider="gmail",
            tenant_id=str(self.tenant.id),
        )
        mock_list_messages.return_value = {
            "messages": [
                {
                    "id": "msg-1",
                    "thread_id": "thread-1",
                    "snippet": "hello",
                    "subject": "Hello",
                    "from": "sender@example.com",
                    "date": "Tue, 10 Feb 2026 10:00:00 +0000",
                    "internal_date": "1739181600000",
                }
            ],
            "result_size_estimate": 1,
        }

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/gmail/messages/?max_results=3",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "gmail")
        self.assertEqual(body["tenant_id"], str(self.tenant.id))
        self.assertEqual(body["result_size_estimate"], 1)
        self.assertEqual(len(body["messages"]), 1)
        mock_broker.assert_called_once_with(tenant=self.tenant, provider="gmail")

    @patch("apps.integrations.runtime_views.get_valid_provider_access_token")
    def test_runtime_gmail_maps_not_connected_error(self, mock_broker):
        mock_broker.side_effect = IntegrationNotConnectedError("missing")

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/gmail/messages/",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "integration_not_connected")

    @patch("apps.integrations.runtime_views.list_gmail_messages")
    @patch("apps.integrations.runtime_views.get_valid_provider_access_token")
    def test_runtime_gmail_maps_provider_http_error(self, mock_broker, mock_list_messages):
        mock_broker.return_value = ProviderAccessToken(
            access_token="access-token",
            expires_at=None,
            provider="gmail",
            tenant_id=str(self.tenant.id),
        )
        req = httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages")
        resp = httpx.Response(403, request=req)
        mock_list_messages.side_effect = httpx.HTTPStatusError(
            "forbidden",
            request=req,
            response=resp,
        )

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/gmail/messages/",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "provider_request_failed")
        self.assertEqual(response.json()["provider_status"], 403)

    @patch("apps.integrations.runtime_views.list_calendar_events")
    @patch("apps.integrations.runtime_views.get_valid_provider_access_token")
    def test_runtime_calendar_returns_events(self, mock_broker, mock_list_events):
        mock_broker.return_value = ProviderAccessToken(
            access_token="access-token",
            expires_at=None,
            provider="google-calendar",
            tenant_id=str(self.tenant.id),
        )
        mock_list_events.return_value = {
            "events": [
                {
                    "id": "evt-1",
                    "summary": "Standup",
                    "status": "confirmed",
                    "html_link": "https://calendar.google.com/event?eid=1",
                    "start": {"dateTime": "2026-02-11T10:00:00Z"},
                    "end": {"dateTime": "2026-02-11T10:30:00Z"},
                }
            ],
            "next_page_token": "",
        }

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/google-calendar/events/?max_results=2",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "google-calendar")
        self.assertEqual(len(body["events"]), 1)
        mock_broker.assert_called_once_with(tenant=self.tenant, provider="google-calendar")

    def test_runtime_calendar_rejects_non_integer_max_results(self):
        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/google-calendar/events/?max_results=abc",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")

    @patch("apps.integrations.runtime_views.get_gmail_message_detail")
    @patch("apps.integrations.runtime_views.get_valid_provider_access_token")
    def test_runtime_gmail_message_detail_returns_payload(self, mock_broker, mock_detail):
        mock_broker.return_value = ProviderAccessToken(
            access_token="access-token",
            expires_at=None,
            provider="gmail",
            tenant_id=str(self.tenant.id),
        )
        mock_detail.return_value = {
            "id": "msg-1",
            "thread_id": "thread-1",
            "snippet": "hello",
            "subject": "Hello",
            "from": "sender@example.com",
            "to": "you@example.com",
            "date": "Tue, 10 Feb 2026 10:00:00 +0000",
            "internal_date": "1739181600000",
            "label_ids": ["INBOX"],
            "body_text": "Please send the report by Friday.",
            "body_truncated": False,
            "thread_context": [],
        }

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/gmail/messages/msg-1/?include_thread=true&thread_limit=3",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "gmail")
        self.assertEqual(body["id"], "msg-1")
        mock_detail.assert_called_once_with(
            access_token="access-token",
            message_id="msg-1",
            include_thread=True,
            thread_limit=3,
        )

    @patch("apps.integrations.runtime_views.get_valid_provider_access_token")
    def test_runtime_gmail_message_detail_maps_scope_error(self, mock_broker):
        mock_broker.side_effect = IntegrationScopeError("scope missing")

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/gmail/messages/msg-1/",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "integration_scope_insufficient")

    @patch("apps.integrations.runtime_views.get_calendar_freebusy")
    @patch("apps.integrations.runtime_views.get_valid_provider_access_token")
    def test_runtime_calendar_freebusy_returns_busy_windows(self, mock_broker, mock_freebusy):
        mock_broker.return_value = ProviderAccessToken(
            access_token="access-token",
            expires_at=None,
            provider="google-calendar",
            tenant_id=str(self.tenant.id),
        )
        mock_freebusy.return_value = {
            "time_min": "2026-02-11T10:00:00Z",
            "time_max": "2026-02-11T18:00:00Z",
            "time_zone": "UTC",
            "busy": [{"start": "2026-02-11T12:00:00Z", "end": "2026-02-11T12:30:00Z"}],
        }

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/google-calendar/freebusy/",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "google-calendar")
        self.assertEqual(len(body["busy"]), 1)
        mock_freebusy.assert_called_once()
