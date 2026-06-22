"""Internal runtime integration endpoint tests."""

from __future__ import annotations

from unittest.mock import patch

import httpx
from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.models import JournalEntry, WeeklyReview
from apps.lessons.models import Lesson, StarJournalEntry, TutoringSession
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .services import (
    IntegrationNotConnectedError,
    IntegrationScopeError,
    ProviderAccessToken,
)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeCronGroundingViewTest(TestCase):
    """Fire-time grounding directive: every custom cron grounds; a cron whose
    message already bakes the full preamble (system seed jobs) is skipped."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Grounding", telegram_chat_id=838383)
        seed_internal_key(self.tenant)

    def _headers(self, tenant_id: str | None = None, key: str = "shared-key") -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def _url(self, name: str) -> str:
        return f"/api/v1/integrations/runtime/{self.tenant.id}/crons/{name}/grounding/"

    def _cron(self, name: str, message: str):
        from apps.cron.models import CronJob

        return CronJob.objects.create(
            tenant=self.tenant,
            name=name,
            data={"payload": {"message": message}},
        )

    def test_requires_internal_auth(self):
        resp = self.client.get(self._url("anything"))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], "internal_auth_failed")

    def test_freeform_cron_with_no_row_is_grounded(self):
        """The freeform case: no CronJob row at all -> must still ground."""
        from apps.orchestrator.config_generator import CRON_GROUNDING_RULE

        resp = self.client.get(self._url("water-the-plants"), **self._headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["inject"])
        self.assertEqual(body["rule"], CRON_GROUNDING_RULE)
        self.assertIn("nbhd_current_status", body["rule"])

    def test_typed_cron_without_baked_preamble_is_grounded(self):
        self._cron("daily-finance-summary", "You are firing a scheduled domain summary. ...")
        resp = self.client.get(self._url("daily-finance-summary"), **self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["inject"])

    def test_internal_sync_cron_is_skipped(self):
        # _sync:/_fuel: are platform-internal, not custom user crons.
        resp = self.client.get(self._url("_sync:test"), **self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["inject"])

    def test_cron_with_baked_preamble_is_skipped(self):
        from apps.orchestrator.config_generator import CRON_PREAMBLE_MARKER

        self._cron(
            "sys-baked",
            f"Current date and time: ...\n\n**MANDATORY - {CRON_PREAMBLE_MARKER}:**\n1. ...",
        )
        resp = self.client.get(self._url("sys-baked"), **self._headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["inject"])
        self.assertEqual(body["rule"], "")


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeCurrentStatusViewTest(TestCase):
    """The current-status projection exposed to the runtime/crons."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Status", telegram_chat_id=818181)
        seed_internal_key(self.tenant)
        self.tenant.experimental_typed_journal_lifecycle = True
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["experimental_typed_journal_lifecycle", "finance_enabled"])

    def _headers(self, tenant_id: str | None = None, key: str = "shared-key") -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def _url(self, tenant_id: str | None = None) -> str:
        return f"/api/v1/integrations/runtime/{tenant_id or self.tenant.id}/current-status/"

    def test_requires_internal_auth(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "internal_auth_failed")

    def test_returns_projection_with_obligation(self):
        from decimal import Decimal

        from apps.finance.models import FinanceAccount

        FinanceAccount.objects.create(
            tenant=self.tenant,
            account_type=FinanceAccount.AccountType.STUDENT_LOAN,
            nickname="Loan AC",
            current_balance=Decimal("1000"),
            minimum_payment=Decimal("25"),
            due_day=5,
        )

        response = self.client.get(self._url(), **self._headers())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tenant_id"], str(self.tenant.id))
        self.assertEqual(
            set(body),
            {
                "tenant_id",
                "as_of",
                "typed_lifecycle",
                "finance_enabled",
                "open_tasks",
                "active_goals",
                "obligations",
            },
        )
        self.assertEqual(len(body["obligations"]), 1)
        self.assertEqual(body["obligations"][0]["nickname"], "Loan AC")


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeIntegrationViewsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Runtime", telegram_chat_id=717171)
        seed_internal_key(self.tenant)
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
            provider="google",
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
        self.assertEqual(body["provider"], "google")
        self.assertEqual(body["tenant_id"], str(self.tenant.id))
        self.assertEqual(body["result_size_estimate"], 1)
        self.assertEqual(len(body["messages"]), 1)
        mock_broker.assert_called_once_with(tenant=self.tenant, provider="google")

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
            provider="google",
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
            provider="google",
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
        self.assertEqual(body["provider"], "google")
        self.assertEqual(len(body["events"]), 1)
        mock_broker.assert_called_once_with(tenant=self.tenant, provider="google")

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
            provider="google",
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
        self.assertEqual(body["provider"], "google")
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
            provider="google",
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
        self.assertEqual(body["provider"], "google")
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
        seed_internal_key(self.tenant)
        # Clear seeded starter docs to test with controlled data only.
        Document.objects.filter(tenant=self.tenant).delete()
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

    @patch("apps.lessons.clustering.refresh_constellation")
    @patch("apps.lessons.services.process_approved_lesson")
    def test_runtime_lessons_create_endpoint_auto_approves_lesson(self, mock_process, mock_refresh):
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
        self.assertEqual(created.status, "approved")
        self.assertIsNotNone(created.approved_at)
        self.assertEqual(created.source_type, "reflection")
        # Embedding + connections run on the auto-approval path.
        mock_process.assert_called_once_with(created)

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


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RedditViewTests(TestCase):
    """Tests for Reddit runtime integration endpoints."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Reddit Test", telegram_chat_id=737373)
        seed_internal_key(self.tenant)

    def _headers(self) -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    @patch("apps.integrations.runtime_views.initiate_composio_connection")
    def test_connect_returns_redirect_url(self, mock_initiate):
        mock_initiate.return_value = ("https://reddit.com/oauth/authorize?state=abc", "req-id-123")

        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/reddit/connect/",
            data={"callback_url": "https://app.example.com/callback"},
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["redirect_url"], "https://reddit.com/oauth/authorize?state=abc")
        self.assertEqual(body["connection_request_id"], "req-id-123")
        mock_initiate.assert_called_once_with(self.tenant, "reddit", "https://app.example.com/callback")

    def test_status_returns_connected_false_when_no_integration(self):
        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/reddit/status/",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["connected"])
        self.assertEqual(body["provider_email"], "")

    def test_status_returns_connected_true_when_active_integration_exists(self):
        from .models import Integration

        Integration.objects.create(
            tenant=self.tenant,
            provider="reddit",
            status=Integration.Status.ACTIVE,
            provider_email="user@reddit.com",
        )

        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/reddit/status/",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["connected"])
        self.assertEqual(body["provider_email"], "user@reddit.com")

    @patch("apps.integrations.runtime_views.disconnect_integration")
    def test_disconnect_calls_service_and_returns_disconnected(self, mock_disconnect):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/reddit/disconnect/",
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["disconnected"])
        mock_disconnect.assert_called_once_with(self.tenant, "reddit")

    @patch("apps.integrations.runtime_views.execute_reddit_tool")
    def test_tool_view_maps_action_to_composio_slug(self, mock_execute):
        mock_execute.return_value = {"posts": [], "count": 0}

        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/reddit/tool/",
            data={"action": "digest", "subreddit": "python", "limit": 5},
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        mock_execute.assert_called_once_with(
            self.tenant,
            "digest",
            {"subreddit": "python", "limit": 5},
        )

    def test_tool_view_rejects_unknown_action(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/reddit/tool/",
            data={"action": "nonexistent_action"},
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_action")

    def test_connect_requires_callback_url(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/reddit/connect/",
            data={},
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 400)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeCronPhase2SummaryViewTest(TestCase):
    """Tool-delegation contract for the Phase 2 sync flow.

    Agent calls ``nbhd_cron_phase2_summary`` with summary + job_name; Django
    constructs the ``_sync:<job>`` one-shot, computes the cron expression,
    and registers it with the OpenClaw gateway. The agent contributes only
    the summary text — every other parameter is server-owned.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Phase2", telegram_chat_id=919191)
        seed_internal_key(self.tenant)

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_requires_internal_auth(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/cron-phase2-summary/",
            data={"summary": "did stuff", "job_name": "Evening Check-in"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_rejects_empty_summary(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/cron-phase2-summary/",
            data={"summary": "  ", "job_name": "Evening Check-in"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "summary_required")

    def test_rejects_missing_job_name(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/cron-phase2-summary/",
            data={"summary": "did stuff"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "job_name_required")

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_success_creates_sync_cron_via_gateway(self, mock_invoke):
        mock_invoke.return_value = {"ok": True}
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/cron-phase2-summary/",
            data={
                "summary": "Wrote today's evening reflection and sent the user a recap.",
                "job_name": "Evening Check-in",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["sync_cron_name"], "_sync:Evening Check-in")

        # Gateway was called with cron.add carrying the canonical sync shape.
        self.assertEqual(mock_invoke.call_count, 1)
        args, _ = mock_invoke.call_args
        self.assertEqual(args[1], "cron.add")
        job = args[2]
        self.assertEqual(job["name"], "_sync:Evening Check-in")
        self.assertEqual(job["sessionTarget"], "main")
        self.assertEqual(job["wakeMode"], "now")
        self.assertEqual(job["payload"]["kind"], "systemEvent")
        # Payload text carries the summary and the self-removal instruction.
        self.assertIn(
            "Wrote today's evening reflection",
            job["payload"]["text"],
        )
        self.assertIn(
            "cron remove _sync:Evening Check-in",
            job["payload"]["text"],
        )
        # Schedule is a one-shot date-specific expression — five parts,
        # last part is the wildcard day-of-week.
        expr = job["schedule"]["expr"]
        self.assertEqual(len(expr.split()), 5)
        self.assertTrue(expr.endswith(" *"))

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_gateway_failure_returns_502(self, mock_invoke):
        from apps.cron.gateway_client import GatewayError

        mock_invoke.side_effect = GatewayError("gateway down")
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/cron-phase2-summary/",
            data={
                "summary": "did stuff",
                "job_name": "Evening Check-in",
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "gateway_failed")


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeJournalContextBackboneDualReadTest(TestCase):
    """journal-context backbone returns typed Goal/Task content when present,
    else falls back to the legacy Document blobs."""

    def setUp(self):
        from apps.journal.models import Document, Goal, Task

        self.tenant = create_tenant(display_name="Backbone", telegram_chat_id=737373)
        seed_internal_key(self.tenant)
        self.Document = Document
        self.Goal = Goal
        self.Task = Task

    def _headers(self) -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _backbone(self) -> dict:
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal-context/",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["backbone"]

    def test_typed_goal_appears_in_goal_backbone(self):
        self.Goal.objects.create(
            tenant=self.tenant,
            title="Pay down loan",
            description="Target zero",
            status=self.Goal.Status.ACTIVE,
        )
        backbone = self._backbone()
        self.assertIn("goal", backbone)
        self.assertIn("Pay down loan", backbone["goal"]["markdown"])

    def test_legacy_goal_document_still_renders_when_no_typed_rows(self):
        # create_tenant seeds a starter Goals Document — overwrite it with
        # non-starter markdown so render_goals' starter-detection returns this.
        self.Document.objects.filter(tenant=self.tenant, kind=self.Document.Kind.GOAL, slug="goals").update(
            markdown="Legacy doc body"
        )
        backbone = self._backbone()
        self.assertIn("goal", backbone)
        self.assertIn("Legacy doc body", backbone["goal"]["markdown"])

    def test_typed_task_appears_in_tasks_backbone(self):
        self.Task.objects.create(
            tenant=self.tenant,
            title="Email accountant",
            status=self.Task.Status.OPEN,
        )
        backbone = self._backbone()
        self.assertIn("tasks", backbone)
        self.assertIn("Email accountant", backbone["tasks"]["markdown"])

    def test_empty_state_omits_goal_and_tasks_when_only_starters_exist(self):
        # Starter Goal/Task Documents are seeded by create_tenant but
        # render_goals / render_open_tasks treat them as empty. The Ideas
        # starter Document is still surfaced as-is (no starter-filter there).
        backbone = self._backbone()
        self.assertNotIn("goal", backbone)
        self.assertNotIn("tasks", backbone)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeJournalContextConstellationTest(TestCase):
    """journal-context surfaces active constellation stars for session init."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Galaxy ctx", telegram_chat_id=646464)
        seed_internal_key(self.tenant)
        Lesson.objects.filter(tenant=self.tenant).delete()

    def _headers(self) -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _get(self) -> dict:
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal-context/",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_constellation_key_omitted_when_no_activity(self):
        Lesson.objects.create(
            tenant=self.tenant,
            text="Untouched",
            context="",
            source_type="reflection",
            source_ref="",
            tags=[],
            status="approved",
        )
        self.assertNotIn("constellation", self._get())

    def test_active_star_appears_in_constellation(self):
        star = Lesson.objects.create(
            tenant=self.tenant,
            text="Rest is productive",
            context="",
            source_type="reflection",
            source_ref="",
            tags=["recovery"],
            status="approved",
            galaxy_note="protect the off-days",
            star_stage="ignited",
        )
        StarJournalEntry.objects.create(
            tenant=self.tenant, star=star, text="Took a full rest day, felt sharper.", entry_type="revisit"
        )
        body = self._get()
        self.assertIn("constellation", body)
        stars = body["constellation"]["active_stars"]
        self.assertEqual(len(stars), 1)
        self.assertEqual(stars[0]["id"], star.id)
        self.assertEqual(stars[0]["galaxy_note"], "protect the off-days")
        self.assertEqual(stars[0]["journal_entries"][0]["entry_type"], "revisit")


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeConstellationNotesViewTest(TestCase):
    """The nbhd_constellation_notes pull surface."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Notes", telegram_chat_id=656565)
        seed_internal_key(self.tenant)
        Lesson.objects.filter(tenant=self.tenant).delete()

    def _headers(self, key="shared-key") -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _star(self, **kwargs) -> Lesson:
        defaults = dict(
            tenant=self.tenant,
            text="Default star",
            context="",
            source_type="reflection",
            source_ref="",
            tags=[],
            status="approved",
        )
        defaults.update(kwargs)
        return Lesson.objects.create(**defaults)

    def _url(self, qs: str = "") -> str:
        return f"/api/v1/integrations/runtime/{self.tenant.id}/constellation/notes/{qs}"

    def test_requires_internal_auth(self):
        self.assertEqual(self.client.get(self._url()).status_code, 401)

    def test_default_mode_returns_recent_active_stars(self):
        star = self._star(text="Name the fear", galaxy_note="say it out loud")
        TutoringSession.objects.create(
            star=star,
            phases_completed=["restate", "deepen", "stress_test"],
            player_found_edge_cases=True,
            mastery_achieved=True,
        )
        resp = self.client.get(self._url(), **self._headers())
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["mode"], "recent")
        self.assertEqual(body["count"], 1)
        node = body["stars"][0]
        self.assertEqual(node["id"], star.id)
        self.assertEqual(node["galaxy_note"], "say it out loud")
        self.assertTrue(node["tutoring_insights"][0]["found_edge_cases"])
        self.assertTrue(node["tutoring_insights"][0]["mastery_achieved"])

    def test_star_id_mode_returns_single_star(self):
        star = self._star(text="One star", galaxy_note="pinned")
        self._star(text="Another", galaxy_note="also pinned")
        resp = self.client.get(self._url(f"?star_id={star.id}"), **self._headers())
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["mode"], "star")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["stars"][0]["id"], star.id)

    def test_bad_star_id_is_rejected(self):
        resp = self.client.get(self._url("?star_id=not-an-int"), **self._headers())
        self.assertEqual(resp.status_code, 400)

    @patch("apps.lessons.services.search_lessons")
    def test_query_mode_uses_search(self, mock_search):
        star = self._star(text="Searchable lesson", galaxy_note="found me")
        mock_search.return_value = [star]
        resp = self.client.get(self._url("?q=focus"), **self._headers())
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["mode"], "search")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["stars"][0]["id"], star.id)
        mock_search.assert_called_once()
