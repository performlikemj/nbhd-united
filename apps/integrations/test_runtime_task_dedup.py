"""End-to-end dedup on the runtime ``nbhd_task_create`` / ``nbhd_goal_create`` path.

Regression guard for the 2026-06-07 resurrection loop: a maintenance/cron agent
turn re-creating a completed task must get the existing row back (200, deduped)
rather than insert a fresh open duplicate.
"""

from __future__ import annotations

import json

from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.models import Goal, Task
from apps.tenants.services import create_tenant


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeTaskCreateDedupTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Dedup", telegram_chat_id=818181)

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _create_task(self, title):
        return self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/",
            data=json.dumps({"title": title}),
            content_type="application/json",
            **self._headers(),
        )

    def test_recreate_of_completed_task_is_deduped(self):
        original = Task.objects.create(
            tenant=self.tenant,
            title="Fill out customs clearance paperwork for Jamaica shipments",
        )
        original.complete()

        resp = self._create_task("Customs clearance paperwork")  # cron's tidied re-derivation

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("deduped"))
        self.assertEqual(body["task"]["id"], str(original.id))
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 1)

    def test_genuinely_new_task_still_creates(self):
        resp = self._create_task("Acknowledge Intuit rejection")
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json().get("deduped", False))
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 1)

    def test_duplicate_open_task_is_deduped(self):
        first = self._create_task("Book hotel for Jamaica wedding")
        self.assertEqual(first.status_code, 201)
        second = self._create_task("Book a hotel for the Jamaica wedding")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json().get("deduped"))
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 1)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeGoalCreateDedupTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="DedupGoal", telegram_chat_id=828282)

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_recreate_of_active_goal_is_deduped(self):
        Goal.objects.create(tenant=self.tenant, title="Achieve debt-free status and financial freedom")
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/goals/",
            data=json.dumps({"title": "Achieve debt-free status"}),
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("deduped"))
        self.assertEqual(Goal.objects.filter(tenant=self.tenant).count(), 1)
