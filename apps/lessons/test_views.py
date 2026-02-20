from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant

from .models import Lesson


class LessonViewSetTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Tenant A",
            telegram_chat_id=100001,
        )
        self.other_tenant = create_tenant(
            display_name="Tenant B",
            telegram_chat_id=100002,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _create_lesson(self, tenant: Tenant, **overrides):
        defaults = {
            "text": "Sample lesson text",
            "context": "conversation",
            "source_type": "conversation",
            "source_ref": "daily-note-1",
            "tags": ["focus", "journal"],
            "status": "pending",
        }
        defaults.update(overrides)
        return Lesson.objects.create(tenant=tenant, **defaults)

    def test_crud_scoped_and_tenant_isolated(self):
        own_lesson = self._create_lesson(
            tenant=self.tenant,
            text="Own lesson",
            status="approved",
        )
        other_lesson = self._create_lesson(
            tenant=self.other_tenant,
            text="Other lesson",
            status="approved",
        )

        list_response = self.client.get("/api/v1/lessons/")
        self.assertEqual(list_response.status_code, 200)
        list_body = list_response.json()
        returned_ids = {item["id"] for item in list_body}
        self.assertEqual(returned_ids, {str(own_lesson.id)})

        retrieve = self.client.get(f"/api/v1/lessons/{own_lesson.id}/")
        self.assertEqual(retrieve.status_code, 200)

        other_retrieve = self.client.get(f"/api/v1/lessons/{other_lesson.id}/")
        self.assertEqual(other_retrieve.status_code, 404)

        update = self.client.patch(
            f"/api/v1/lessons/{own_lesson.id}/",
            {"text": "Updated lesson"},
            format="json",
        )
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()["text"], "Updated lesson")

        delete = self.client.delete(f"/api/v1/lessons/{own_lesson.id}/")
        self.assertEqual(delete.status_code, 204)
        self.assertFalse(Lesson.objects.filter(id=own_lesson.id).exists())

    def test_create_assigns_tenant_scopes_automatically(self):
        response = self.client.post(
            "/api/v1/lessons/",
            {
                "text": "New lesson",
                "context": "my context",
                "source_type": "journal",
                "source_ref": "entry-42",
                "tags": ["productivity", "mindset"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        created = Lesson.objects.get(id=response.json()["id"])
        self.assertEqual(created.tenant_id, self.tenant.id)
        self.assertEqual(created.status, "pending")

    def test_approve_and_dismiss_flow(self):
        lesson = self._create_lesson(tenant=self.tenant, text="Need approval", status="pending")

        approve = self.client.post(
            f"/api/v1/lessons/{lesson.id}/approve/",
            {"status": "approved"},
            format="json",
        )
        self.assertEqual(approve.status_code, 200)

        lesson.refresh_from_db()
        self.assertEqual(lesson.status, "approved")
        self.assertIsNotNone(lesson.approved_at)

        dismiss = self.client.post(
            f"/api/v1/lessons/{lesson.id}/dismiss/",
            {"status": "dismissed"},
            format="json",
        )
        self.assertEqual(dismiss.status_code, 200)

        lesson.refresh_from_db()
        self.assertEqual(lesson.status, "dismissed")
        self.assertIsNone(lesson.approved_at)

    def test_tenant_isolation_between_users(self):
        lesson_other = self._create_lesson(tenant=self.other_tenant, status="approved")

        list_response = self.client.get("/api/v1/lessons/")
        self.assertEqual(list_response.status_code, 200)
        self.assertNotIn(str(lesson_other.id), {item["id"] for item in list_response.json()})

        detail = self.client.get(f"/api/v1/lessons/{lesson_other.id}/")
        self.assertEqual(detail.status_code, 404)

    def test_pending_action_filters_pending_only(self):
        pending = self._create_lesson(tenant=self.tenant, text="First", status="pending")
        self._create_lesson(tenant=self.tenant, text="Approved", status="approved")

        response = self.client.get("/api/v1/lessons/pending/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["id"], str(pending.id))
