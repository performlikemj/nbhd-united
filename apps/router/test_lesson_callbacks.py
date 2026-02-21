"""Tests for Telegram lesson callback handling."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.lessons.models import Lesson
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


@override_settings(TELEGRAM_WEBHOOK_SECRET="test-secret", ROUTER_RATE_LIMIT_PER_MINUTE=10)
class LessonCallbackTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Lesson Tenant", telegram_chat_id=987111)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_fqdn = "oc-lessons.internal.azurecontainerapps.io"
        self.tenant.save(update_fields=["status", "container_fqdn", "updated_at"])

    def _post_update(self, payload: dict) -> object:
        return self.client.post(
            "/api/v1/telegram/webhook/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-secret",
        )

    @patch("apps.router.lesson_callbacks._edit_message_text")
    @patch("apps.router.lesson_callbacks.process_approved_lesson")
    def test_approve_callback_updates_lesson_and_edits_message(self, mock_process, mock_edit_message):
        lesson = Lesson.objects.create(
            tenant=self.tenant,
            text="This is a very insightful lesson about miso that can be approved inline.",
            context="cooking notes",
            source_type="experience",
            status="pending",
        )
        mock_process.return_value = None

        response = self._post_update(
            {
                "callback_query": {
                    "id": "cbq-1",
                    "data": f"lesson:approve:{lesson.id}",
                    "message": {
                        "chat": {"id": self.tenant.user.telegram_chat_id},
                        "message_id": 42,
                    },
                }
            }
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["method"], "answerCallbackQuery")
        self.assertEqual(payload["callback_query_id"], "cbq-1")
        self.assertEqual(payload["text"], "Added to your learning graph!")

        lesson.refresh_from_db()
        self.assertEqual(lesson.status, "approved")
        self.assertIsNotNone(lesson.approved_at)
        mock_edit_message.assert_called_once_with(
            self.tenant.user.telegram_chat_id,
            42,
            "✅ Approved: This is a very insightful lesson about miso that can be approved inline.",
        )
        mock_process.assert_called_once_with(lesson)

    @patch("apps.router.lesson_callbacks._edit_message_text")
    def test_dismiss_callback_updates_lesson_and_edits_message(self, mock_edit_message):
        lesson = Lesson.objects.create(
            tenant=self.tenant,
            text="A small lesson to dismiss",
            context="reflection",
            source_type="reflection",
            status="pending",
        )

        response = self._post_update(
            {
                "callback_query": {
                    "id": "cbq-2",
                    "data": f"lesson:dismiss:{lesson.id}",
                    "message": {
                        "chat": {"id": self.tenant.user.telegram_chat_id},
                        "message_id": 43,
                    },
                }
            }
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["method"], "answerCallbackQuery")
        self.assertEqual(payload["text"], "Dismissed")

        lesson.refresh_from_db()
        self.assertEqual(lesson.status, "dismissed")
        mock_edit_message.assert_called_once_with(
            self.tenant.user.telegram_chat_id,
            43,
            "❌ Dismissed: A small lesson to dismiss",
        )

    def test_invalid_lesson_id_returns_error(self):
        response = self._post_update(
            {
                "callback_query": {
                    "id": "cbq-3",
                    "data": "lesson:approve:not-an-id",
                    "message": {
                        "chat": {"id": self.tenant.user.telegram_chat_id},
                        "message_id": 44,
                    },
                }
            }
        )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["method"], "answerCallbackQuery")
        self.assertIn("Invalid", payload["text"])

    def test_already_processed_lesson_returns_already_processed(self):
        lesson = Lesson.objects.create(
            tenant=self.tenant,
            text="Already approved once",
            context="session",
            source_type="conversation",
            status="approved",
            approved_at=timezone.now(),
        )

        response = self._post_update(
            {
                "callback_query": {
                    "id": "cbq-4",
                    "data": f"lesson:approve:{lesson.id}",
                    "message": {
                        "chat": {"id": self.tenant.user.telegram_chat_id},
                        "message_id": 45,
                    },
                }
            }
        )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["method"], "answerCallbackQuery")
        self.assertIn("already processed", payload["text"])

    @patch("apps.router.views.forward_to_openclaw", new_callable=AsyncMock)
    def test_non_lesson_callback_data_is_forwarded_to_openclaw(self, mock_forward):
        mock_forward.return_value = {"ok": True}

        response = self._post_update(
            {
                "callback_query": {
                    "id": "cbq-5",
                    "data": "not-lesson:foo:123",
                    "message": {
                        "chat": {"id": self.tenant.user.telegram_chat_id},
                        "message_id": 99,
                    },
                }
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        mock_forward.assert_awaited_once()
        call_kwargs = mock_forward.await_args.kwargs
        self.assertEqual(call_kwargs.get("user_timezone"), "UTC")
        call_args = mock_forward.await_args.args
        self.assertEqual(call_args[0], self.tenant.container_fqdn)
        self.assertEqual(call_kwargs.get("timeout"), 30.0)
        self.assertEqual(call_kwargs.get("max_retries"), 1)
        self.assertEqual(call_kwargs.get("retry_delay"), 5.0)
        self.assertEqual(call_kwargs.get("user_timezone"), "UTC")
