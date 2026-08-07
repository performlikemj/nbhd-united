"""Two-phase confirm handshake on ``DELETE`` of a runtime task.

``nbhd_task_delete`` is the only irreversible tool on the runtime surface: there
is no archived status, no undo, and ``Task.parent_task`` is ``CASCADE`` so a
parent takes its whole subtree with it. The structural handshake — the server
refusing to delete anything without ``confirm: true`` — IS the safety mechanism,
not the tool description, so these tests pin it from both directions:

  * the un-confirmed phase must be completely inert (nothing deleted, nothing
    mutated), because it is the last thing between a misread sentence and
    permanent data loss; and
  * the confirmed phase must destroy exactly what the preview promised — the
    subtask count the user agreed to has to be the count that actually goes.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings
from rest_framework import status as http_status
from rest_framework.response import Response

from apps.journal.models import Task
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeTaskDeleteHandshakeTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="TaskDelete", telegram_chat_id=919191)
        seed_internal_key(self.tenant)
        self.task = Task.objects.create(tenant=self.tenant, title="Renew the shipping licence")

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _delete(self, task_id, body=None, **extra):
        headers = {**self._headers(), **extra}
        return self.client.delete(
            f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/{task_id}/",
            data=json.dumps(body if body is not None else {}),
            content_type="application/json",
            **headers,
        )

    # ── phase 1: preview ────────────────────────────────────────────────────

    def test_no_confirm_returns_confirmation_required(self):
        resp = self._delete(self.task.id)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "confirmation_required")
        self.assertEqual(body["subtask_count"], 0)
        self.assertEqual(body["task"]["id"], str(self.task.id))
        self.assertEqual(body["task"]["title"], "Renew the shipping licence")
        self.assertEqual(body["task"]["status"], "open")
        self.assertIn("confirm=true", body["hint"])

    def test_no_confirm_deletes_nothing(self):
        """The load-bearing guarantee. If this ever fails, the tool is a
        one-call data-loss bug: the model would destroy a task while it thought
        it was only asking the user about it.
        """
        before = set(Task.objects.values_list("id", flat=True))

        resp = self._delete(self.task.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(Task.objects.values_list("id", flat=True)), before)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.OPEN)

    def test_repeated_previews_stay_inert(self):
        for _ in range(3):
            self.assertEqual(self._delete(self.task.id).status_code, 200)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_preview_exposes_only_id_title_status(self):
        """No owner metadata rides along. The runtime serializer is used without
        the ``rehydrate`` flag, so the title stays in placeholder space — what
        the model already has — and ``pii_receipts`` (which the rehydrating path
        attaches) can never appear.
        """
        body = self._delete(self.task.id).json()

        self.assertEqual(set(body["task"].keys()), {"id", "title", "status"})
        self.assertEqual(set(body.keys()), {"status", "task", "subtask_count", "hint"})

    def test_preview_title_is_the_stored_placeholder_space_value(self):
        self.task.title = "Call [PERSON_1] about the lease"
        self.task.save(update_fields=["title"])

        body = self._delete(self.task.id).json()

        self.assertEqual(body["task"]["title"], "Call [PERSON_1] about the lease")

    # ── confirm must be a real boolean ──────────────────────────────────────

    def test_stringy_confirm_does_not_delete(self):
        """Fails closed. A string "true" costs one extra round trip; treating it
        as consent costs the user's data.
        """
        resp = self._delete(self.task.id, {"confirm": "true"})

        self.assertEqual(resp.json()["status"], "confirmation_required")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_truthy_non_boolean_confirm_does_not_delete(self):
        for value in (1, "yes", ["true"], {"confirm": True}):
            with self.subTest(value=value):
                resp = self._delete(self.task.id, {"confirm": value})
                self.assertEqual(resp.json()["status"], "confirmation_required")
                self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_confirm_false_does_not_delete(self):
        resp = self._delete(self.task.id, {"confirm": False})

        self.assertEqual(resp.json()["status"], "confirmation_required")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_empty_body_does_not_delete(self):
        resp = self.client.delete(
            f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/{self.task.id}/",
            **self._headers(),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "confirmation_required")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    # ── phase 2: the actual delete ──────────────────────────────────────────

    def test_confirm_deletes_the_task(self):
        resp = self._delete(self.task.id, {"confirm": True})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "deleted")
        self.assertEqual(body["id"], str(self.task.id))
        self.assertEqual(body["subtasks_deleted"], 0)
        self.assertFalse(Task.objects.filter(id=self.task.id).exists())

    def test_confirm_deletes_subtasks_and_reports_the_count(self):
        children = [Task.objects.create(tenant=self.tenant, title=f"Step {n}", parent_task=self.task) for n in range(3)]

        preview = self._delete(self.task.id).json()
        self.assertEqual(preview["subtask_count"], 3)

        resp = self._delete(self.task.id, {"confirm": True})

        self.assertEqual(resp.json()["subtasks_deleted"], 3)
        self.assertFalse(Task.objects.filter(id__in=[c.id for c in children]).exists())
        self.assertFalse(Task.objects.filter(id=self.task.id).exists())

    def test_count_covers_the_whole_subtree_not_just_direct_children(self):
        """CASCADE is recursive, so a grandchild dies too. A count that stopped
        at one level would understate the damage in the very prompt the user
        uses to decide.
        """
        child = Task.objects.create(tenant=self.tenant, title="Child", parent_task=self.task)
        grandchild = Task.objects.create(tenant=self.tenant, title="Grandchild", parent_task=child)

        self.assertEqual(self._delete(self.task.id).json()["subtask_count"], 2)

        resp = self._delete(self.task.id, {"confirm": True})

        self.assertEqual(resp.json()["subtasks_deleted"], 2)
        self.assertFalse(Task.objects.filter(id__in=[child.id, grandchild.id]).exists())

    def test_deleting_a_subtask_leaves_its_parent_alone(self):
        child = Task.objects.create(tenant=self.tenant, title="Child", parent_task=self.task)

        resp = self._delete(child.id, {"confirm": True})

        self.assertEqual(resp.json()["subtasks_deleted"], 0)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_preview_count_matches_what_the_delete_reports(self):
        for n in range(4):
            Task.objects.create(tenant=self.tenant, title=f"Sub {n}", parent_task=self.task)

        previewed = self._delete(self.task.id).json()["subtask_count"]
        deleted = self._delete(self.task.id, {"confirm": True}).json()["subtasks_deleted"]

        self.assertEqual(previewed, deleted)

    def test_confirm_does_not_touch_unrelated_tasks(self):
        bystander = Task.objects.create(tenant=self.tenant, title="Unrelated")

        self._delete(self.task.id, {"confirm": True})

        self.assertTrue(Task.objects.filter(id=bystander.id).exists())

    def test_second_confirm_is_a_404(self):
        self._delete(self.task.id, {"confirm": True})

        resp = self._delete(self.task.id, {"confirm": True})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "task_not_found")

    # ── auth and tenancy ────────────────────────────────────────────────────

    def test_missing_internal_key_is_401(self):
        resp = self.client.delete(
            f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/{self.task.id}/",
            data=json.dumps({"confirm": True}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 401)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_wrong_internal_key_is_401(self):
        resp = self._delete(
            self.task.id,
            {"confirm": True},
            HTTP_X_NBHD_INTERNAL_KEY="not-the-key",
        )

        self.assertEqual(resp.status_code, 401)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_another_tenants_task_is_404_and_survives(self):
        other = create_tenant(display_name="Other", telegram_chat_id=929292)
        seed_internal_key(other)
        victim = Task.objects.create(tenant=other, title="Not yours")

        resp = self._delete(victim.id, {"confirm": True})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "task_not_found")
        self.assertTrue(Task.objects.filter(id=victim.id).exists())

    def test_another_tenants_subtree_is_not_counted(self):
        """The descendant walk is tenant-scoped: a cross-tenant ``parent_task``
        row must not inflate the count, nor be reachable for deletion.
        """
        other = create_tenant(display_name="OtherTree", telegram_chat_id=939393)
        Task.objects.create(tenant=other, title="Foreign child", parent_task=self.task)

        self.assertEqual(self._delete(self.task.id).json()["subtask_count"], 0)

    # ── the document-turn write guard ───────────────────────────────────────

    def test_document_turn_guard_blocks_the_delete(self):
        """Deletion is a destination write, so the prompt-injection backstop
        applies exactly as it does to task creation. Patched to return its
        refusal so the test proves the guard is consulted BEFORE anything is
        destroyed, not merely that it is called.
        """
        refusal = Response({"error": "document_turn_write_blocked"}, status=http_status.HTTP_409_CONFLICT)
        with patch(
            "apps.integrations.runtime_views.assert_write_allowed_for_document_turn",
            return_value=refusal,
        ):
            resp = self._delete(self.task.id, {"confirm": True})

        self.assertEqual(resp.status_code, 409)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())
