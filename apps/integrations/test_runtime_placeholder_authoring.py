from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.journal.models import Task
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimePlaceholderAuthoringTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="runtime-authoring", password="x")
        self.tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
        seed_internal_key(self.tenant, key="shared-key")
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_flag_off_create_is_passthrough_with_bypass_receipts(self):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/",
            {"title": "  Alice task  ", "description": "Alice desc"},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        task = Task.objects.get(id=response.json()["task"]["id"])
        self.assertEqual(task.title, "Alice task")  # serializer trims surrounding whitespace only
        self.assertEqual(task.description, "Alice desc")
        self.assertEqual(task.pii_receipts["title"], {"state": "bypass"})
        self.assertNotIn("pii_receipts", response.json()["task"])

    def test_flag_on_create_patch_and_transition_stay_placeholder_space(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            created = self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/",
                {"title": "Call Alice"},
                format="json",
                **self.headers,
            )
            task_id = created.json()["task"]["id"]
            patched = self.client.patch(
                f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/{task_id}/",
                {"description": "Plan with Alice"},
                format="json",
                **self.headers,
            )
            transitioned = self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/{task_id}/complete/",
                format="json",
                **self.headers,
            )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(transitioned.status_code, 200)
        task = Task.objects.get(id=task_id)
        self.assertEqual(task.title, "Call [PERSON_1]")
        self.assertEqual(task.description, "Plan with [PERSON_1]")
        self.assertEqual(task.pii_receipts["title"]["state"], "placeholder")
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertEqual(transitioned.json()["task"]["title"], "Call [PERSON_1]")
        self.assertNotIn("pii_receipts", transitioned.json()["task"])
