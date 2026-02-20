"""Internal runtime integration endpoint tests."""
from __future__ import annotations

from unittest.mock import patch

import httpx
from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.models import JournalEntry, WeeklyReview
from apps.lessons.models import Lesson
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

    def test_runtime_journal_requires_internal_auth(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal-entries/",
            data={},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "internal_auth_failed")

    def test_runtime_journal_create_entry_persists_tenant_scoped(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal-entries/",
            data={
                "date": "2026-02-12",
                "mood": "focused",
                "energy": "medium",
                "wins": ["Shipped patch"],
                "challenges": ["Too many meetings"],
                "reflection": "Block deep work tomorrow",
                "raw_text": "Conversation summary",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["tenant_id"], str(self.tenant.id))
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.first().tenant, self.tenant)

    def test_runtime_journal_rejects_invalid_energy(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal-entries/",
            data={
                "date": "2026-02-12",
                "mood": "focused",
                "energy": "turbo",
                "wins": [],
                "challenges": [],
                "raw_text": "Conversation summary",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("energy", response.json())

    def test_runtime_journal_list_entries_filters_by_date_range(self):
        JournalEntry.objects.create(
            tenant=self.tenant,
            date="2026-02-10",
            mood="good",
            energy="high",
            wins=["A"],
            challenges=[],
            raw_text="entry-a",
        )
        JournalEntry.objects.create(
            tenant=self.tenant,
            date="2026-02-12",
            mood="tired",
            energy="low",
            wins=[],
            challenges=["B"],
            raw_text="entry-b",
        )
        JournalEntry.objects.create(
            tenant=self.other_tenant,
            date="2026-02-11",
            mood="other",
            energy="medium",
            wins=["hidden"],
            challenges=[],
            raw_text="entry-hidden",
        )

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal-entries/?date_from=2026-02-12&date_to=2026-02-12",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["entries"][0]["date"], "2026-02-12")

    def test_runtime_journal_rejects_invalid_date_window(self):
        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal-entries/?date_from=2026-02-13&date_to=2026-02-12",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")

    def test_runtime_weekly_review_create_persists(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/weekly-reviews/",
            data={
                "week_start": "2026-02-06",
                "week_end": "2026-02-12",
                "mood_summary": "Strong finish",
                "top_wins": ["Ship feature"],
                "top_challenges": ["Context switching"],
                "lessons": ["Batch meetings"],
                "week_rating": "thumbs-up",
                "intentions_next_week": ["Protect mornings"],
                "raw_text": "Review summary",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(WeeklyReview.objects.count(), 1)
        self.assertEqual(WeeklyReview.objects.first().tenant, self.tenant)

    def test_runtime_weekly_review_rejects_invalid_rating(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/weekly-reviews/",
            data={
                "week_start": "2026-02-06",
                "week_end": "2026-02-12",
                "mood_summary": "Strong finish",
                "top_wins": [],
                "top_challenges": [],
                "lessons": [],
                "week_rating": "great",
                "intentions_next_week": ["Protect mornings"],
                "raw_text": "Review summary",
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("week_rating", response.json())


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeMemorySyncViewTest(TestCase):
    def setUp(self):
        from apps.journal.models import Document

        self.tenant = create_tenant(display_name="SyncTest", telegram_chat_id=818181)
        Document.objects.create(
            tenant=self.tenant,
            kind="memory",
            slug="long-term",
            title="Long-Term Memory",
            markdown="Important context",
        )

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def test_requires_auth(self):
        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/memory-sync/",
        )
        self.assertEqual(response.status_code, 401)

    def test_returns_files(self):
        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/memory-sync/",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tenant_id"], str(self.tenant.id))
        self.assertEqual(body["count"], 1)
        self.assertIn("memory/journal/memory/long-term.md", body["files"])
        self.assertIn("# Long-Term Memory", body["files"]["memory/journal/memory/long-term.md"])


    def test_runtime_lessons_create_endpoint_creates_pending_lesson(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/lessons/",
            data={
                "text": "Keep your promises to yourself",
                "context": "evening check-in",
                "source_type": "reflection",
                "source_ref": "conversation:msg-1",
                "tags": ["growth", "discipline"],
            },
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 201)
        created = Lesson.objects.filter(tenant=self.tenant).get(text="Keep your promises to yourself")
        self.assertEqual(created.status, "pending")
        self.assertEqual(created.source_type, "reflection")

    @patch("apps.integrations.runtime_views.search_lessons")
    def test_runtime_lesson_search_endpoint_supports_query(self, mock_search):
        lesson = Lesson.objects.create(
            tenant=self.tenant,
            text="Focus creates momentum",
            context="reflection",
            source_type="reflection",
            source_ref="",
            tags=["focus"],
            status="approved",
        )
        lesson.similarity = 0.87
        mock_search.return_value = [lesson]

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/lessons/search/?q=focus&limit=5",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["id"], lesson.id)

    def test_runtime_lessons_pending_endpoint_lists_tenant_pending(self):
        Lesson.objects.create(
            tenant=self.tenant,
            text="Pending one",
            context="",
            source_type="conversation",
            source_ref="",
            tags=["x"],
            status="pending",
        )

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/lessons/pending/",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["lessons"][0]["text"], "Pending one")
