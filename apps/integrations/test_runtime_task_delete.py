"""Two-phase confirm handshake on ``DELETE`` of a runtime task.

``nbhd_task_delete`` is the only irreversible tool on the runtime surface: there
is no archived status, no undo, and ``Task.parent_task`` is ``CASCADE`` so a
parent takes its whole subtree with it. The structural handshake — the server
refusing to delete anything without ``confirm: true`` — IS the safety mechanism,
not the tool description, so these tests pin it from several directions:

  * the un-confirmed phase must be completely inert (nothing deleted, nothing
    mutated), because it is the last thing between a misread sentence and
    permanent data loss;
  * the confirmed phase must be BOUND to the preview the user actually saw —
    ``expected_subtask_count`` must match a freshly recomputed count, so a "yes"
    given about three subtasks can never destroy four; and
  * the confirmed phase must destroy exactly what the preview promised, and
    refuse outright when the cascade would reach outside the tenant.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings
from rest_framework import status as http_status
from rest_framework.response import Response

from apps.journal.models import PendingTaskAction, Task
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

    def _url(self, task_id):
        return f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/{task_id}/"

    def _delete(self, task_id, body=None, **extra):
        headers = {**self._headers(), **extra}
        return self.client.delete(
            self._url(task_id),
            data=json.dumps(body if body is not None else {}),
            content_type="application/json",
            **headers,
        )

    def _confirm(self, task_id, expected, **extra):
        """The happy-path phase 2: confirm bound to the count the preview showed."""
        return self._delete(task_id, {"confirm": True, "expected_subtask_count": expected}, **extra)

    # ── phase 1: preview ────────────────────────────────────────────────────

    def test_no_confirm_returns_confirmation_required(self):
        resp = self._delete(self.task.id)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "confirmation_required")
        self.assertEqual(body["subtask_count"], 0)
        self.assertEqual(body["pending_action_count"], 0)
        self.assertEqual(body["task"]["id"], str(self.task.id))
        self.assertEqual(body["task"]["title"], "Renew the shipping licence")
        self.assertEqual(body["task"]["status"], "open")
        self.assertIn("confirm=true", body["hint"])
        # The hint has to name the binding parameter, or the model has no way to
        # learn it needs one until the server rejects the call.
        self.assertIn("expected_subtask_count=0", body["hint"])

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
        self.assertEqual(
            set(body.keys()),
            {"status", "task", "subtask_count", "pending_action_count", "hint"},
        )

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
        resp = self._delete(self.task.id, {"confirm": "true", "expected_subtask_count": 0})

        self.assertEqual(resp.json()["status"], "confirmation_required")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_truthy_non_boolean_confirm_does_not_delete(self):
        for value in (1, "yes", ["true"], {"confirm": True}):
            with self.subTest(value=value):
                resp = self._delete(self.task.id, {"confirm": value, "expected_subtask_count": 0})
                self.assertEqual(resp.json()["status"], "confirmation_required")
                self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_confirm_false_does_not_delete(self):
        resp = self._delete(self.task.id, {"confirm": False, "expected_subtask_count": 0})

        self.assertEqual(resp.json()["status"], "confirmation_required")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_empty_body_does_not_delete(self):
        resp = self.client.delete(self._url(self.task.id), **self._headers())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "confirmation_required")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    # ── the confirm is bound to the preview the user saw ────────────────────

    def test_confirm_without_expected_count_is_refused(self):
        resp = self._delete(self.task.id, {"confirm": True})

        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["status"], "confirmation_required")
        self.assertEqual(body["reason"], "count_changed")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_confirm_with_wrong_expected_count_is_refused(self):
        resp = self._confirm(self.task.id, 7)

        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["reason"], "count_changed")
        # The refusal carries the FRESH count so the model can re-ask accurately.
        self.assertEqual(body["subtask_count"], 0)
        self.assertIn("hint", body)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_a_subtask_added_between_preview_and_confirm_blocks_the_delete(self):
        """The whole point of the binding. The user agreed to delete a task with
        two subtasks; by the time the model confirms there are three. The third
        was never shown to anyone, so the "yes" does not cover it.
        """
        first = Task.objects.create(tenant=self.tenant, title="Step 1", parent_task=self.task)
        second = Task.objects.create(tenant=self.tenant, title="Step 2", parent_task=self.task)

        previewed = self._delete(self.task.id).json()["subtask_count"]
        self.assertEqual(previewed, 2)

        # Someone (the user on another device, a cron turn) adds a third.
        late = Task.objects.create(tenant=self.tenant, title="Step 3", parent_task=self.task)

        stale = self._confirm(self.task.id, previewed)

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["reason"], "count_changed")
        self.assertEqual(stale.json()["subtask_count"], 3)
        # NOTHING was destroyed.
        for row in (self.task, first, second, late):
            self.assertTrue(Task.objects.filter(id=row.id).exists(), f"{row.title} must survive")

        # Re-previewed and re-confirmed against the true count, it goes.
        fresh = self._delete(self.task.id).json()["subtask_count"]
        self.assertEqual(fresh, 3)
        resp = self._confirm(self.task.id, fresh)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["subtasks_deleted"], 3)
        self.assertFalse(Task.objects.filter(id__in=[self.task.id, first.id, second.id, late.id]).exists())

    def test_expected_count_true_does_not_satisfy_a_count_of_one(self):
        """``isinstance(True, int)`` is True in Python. A bool must not be
        accepted as the number 1.
        """
        Task.objects.create(tenant=self.tenant, title="Only child", parent_task=self.task)

        resp = self._delete(self.task.id, {"confirm": True, "expected_subtask_count": True})

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["reason"], "count_changed")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_expected_count_as_string_is_refused(self):
        resp = self._delete(self.task.id, {"confirm": True, "expected_subtask_count": "0"})

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["reason"], "count_changed")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    # ── phase 2: the actual delete ──────────────────────────────────────────

    def test_confirm_deletes_the_task(self):
        resp = self._confirm(self.task.id, 0)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "deleted")
        self.assertEqual(body["id"], str(self.task.id))
        self.assertEqual(body["subtasks_deleted"], 0)
        self.assertEqual(body["pending_actions_deleted"], 0)
        self.assertFalse(Task.objects.filter(id=self.task.id).exists())

    def test_confirm_deletes_subtasks_and_reports_the_count(self):
        children = [Task.objects.create(tenant=self.tenant, title=f"Step {n}", parent_task=self.task) for n in range(3)]

        preview = self._delete(self.task.id).json()
        self.assertEqual(preview["subtask_count"], 3)

        resp = self._confirm(self.task.id, 3)

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

        resp = self._confirm(self.task.id, 2)

        self.assertEqual(resp.json()["subtasks_deleted"], 2)
        self.assertFalse(Task.objects.filter(id__in=[child.id, grandchild.id]).exists())

    def test_deleting_a_subtask_leaves_its_parent_alone(self):
        child = Task.objects.create(tenant=self.tenant, title="Child", parent_task=self.task)

        resp = self._confirm(child.id, 0)

        self.assertEqual(resp.json()["subtasks_deleted"], 0)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_preview_count_matches_what_the_delete_reports(self):
        for n in range(4):
            Task.objects.create(tenant=self.tenant, title=f"Sub {n}", parent_task=self.task)

        previewed = self._delete(self.task.id).json()["subtask_count"]
        deleted = self._confirm(self.task.id, previewed).json()["subtasks_deleted"]

        self.assertEqual(previewed, deleted)

    def test_confirm_does_not_touch_unrelated_tasks(self):
        bystander = Task.objects.create(tenant=self.tenant, title="Unrelated")

        self._confirm(self.task.id, 0)

        self.assertTrue(Task.objects.filter(id=bystander.id).exists())

    def test_second_confirm_is_a_404(self):
        self._confirm(self.task.id, 0)

        resp = self._confirm(self.task.id, 0)

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "task_not_found")

    # ── the PendingTaskAction audit trail cascades too ──────────────────────

    def _seed_pending_action(self, task, kind=PendingTaskAction.Kind.TASK_COMPLETE):
        return PendingTaskAction.objects.create(
            tenant=self.tenant,
            kind=kind,
            task=task,
            source_date=date(2026, 8, 8),
        )

    def test_preview_counts_pending_actions_across_the_whole_cascade(self):
        child = Task.objects.create(tenant=self.tenant, title="Child", parent_task=self.task)
        self._seed_pending_action(self.task)
        self._seed_pending_action(child)
        self._seed_pending_action(child, PendingTaskAction.Kind.TASK_DEFER)
        # A bystander's audit row must NOT be counted.
        self._seed_pending_action(Task.objects.create(tenant=self.tenant, title="Bystander"))

        body = self._delete(self.task.id).json()

        self.assertEqual(body["subtask_count"], 1)
        self.assertEqual(body["pending_action_count"], 3)

    def test_confirm_reports_and_destroys_the_pending_actions(self):
        child = Task.objects.create(tenant=self.tenant, title="Child", parent_task=self.task)
        doomed = [self._seed_pending_action(self.task), self._seed_pending_action(child)]
        survivor = self._seed_pending_action(Task.objects.create(tenant=self.tenant, title="Bystander"))

        resp = self._confirm(self.task.id, 1)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pending_actions_deleted"], 2)
        self.assertFalse(PendingTaskAction.objects.filter(id__in=[p.id for p in doomed]).exists())
        self.assertTrue(PendingTaskAction.objects.filter(id=survivor.id).exists())

    # ── auth and tenancy ────────────────────────────────────────────────────

    def test_missing_internal_key_is_401(self):
        resp = self.client.delete(
            self._url(self.task.id),
            data=json.dumps({"confirm": True, "expected_subtask_count": 0}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 401)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_wrong_internal_key_is_401(self):
        resp = self._confirm(self.task.id, 0, HTTP_X_NBHD_INTERNAL_KEY="not-the-key")

        self.assertEqual(resp.status_code, 401)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_another_tenants_task_is_404_and_survives(self):
        other = create_tenant(display_name="Other", telegram_chat_id=929292)
        seed_internal_key(other)
        victim = Task.objects.create(tenant=other, title="Not yours")

        resp = self._confirm(victim.id, 0)

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "task_not_found")
        self.assertTrue(Task.objects.filter(id=victim.id).exists())

    def test_a_cross_tenant_subtask_makes_the_delete_refuse(self):
        """A foreign child hanging off our task is unreachable by the
        tenant-scoped count but NOT by the ORM cascade, which ignores that
        filter entirely. Deleting would destroy a stranger's row and report a
        number that excluded it, so the server refuses instead — and both tasks
        survive the refusal.
        """
        other = create_tenant(display_name="OtherTree", telegram_chat_id=939393)
        foreign_child = Task.objects.create(tenant=other, title="Foreign child", parent_task=self.task)

        # The preview still counts zero — that is exactly the discrepancy the
        # confirm leg exists to catch.
        preview = self._delete(self.task.id).json()
        self.assertEqual(preview["subtask_count"], 0)

        resp = self._confirm(self.task.id, 0)

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "integrity_error")
        self.assertTrue(Task.objects.filter(id=foreign_child.id).exists(), "the foreign row must survive")
        self.assertTrue(Task.objects.filter(id=self.task.id).exists(), "our own row must survive too")

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
            resp = self._confirm(self.task.id, 0)

        self.assertEqual(resp.status_code, 409)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeTaskPatchDeleteRaceTest(TestCase):
    """A PATCH that lost the race to a DELETE must 404, not resurrect the row.

    ``serializer.save()`` issues an UPDATE with no ``update_fields``; when that
    UPDATE matches zero rows Django falls through to an INSERT, and ``Task.id``
    carries a client-side ``uuid4`` default, so the INSERT succeeds. The deleted
    task reappears — same id, same content, no longer referenced by anything
    that deleted it. The fix re-reads under ``select_for_update()`` inside the
    write transaction; these tests drive the exact interleaving.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="PatchRace", telegram_chat_id=949494)
        seed_internal_key(self.tenant)
        self.task = Task.objects.create(tenant=self.tenant, title="Original title")

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _patch(self, body):
        return self.client.patch(
            f"/api/v1/integrations/runtime/{self.tenant.id}/tasks/{self.task.id}/",
            data=json.dumps(body),
            content_type="application/json",
            **self._headers(),
        )

    def test_patch_after_a_concurrent_delete_does_not_resurrect_the_task(self):
        task_id = self.task.id

        def delete_the_row_mid_request(*args, **kwargs):
            # Runs after the view's first read and before it opens the write
            # transaction — precisely the window a concurrent delete occupies.
            Task.objects.filter(id=task_id).delete()
            return ({"title": "Resurrected"}, {})

        with patch(
            "apps.integrations.runtime_views._author_runtime_lifecycle_input",
            side_effect=delete_the_row_mid_request,
        ):
            resp = self._patch({"title": "Resurrected"})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "task_not_found")
        self.assertFalse(Task.objects.filter(id=task_id).exists(), "the task must stay deleted")

    def test_ordinary_patch_still_works(self):
        """The lock must not break the normal path."""
        resp = self._patch({"title": "Updated title"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["task"]["title"], "Updated title")
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Updated title")

    def test_sequential_patches_on_different_fields_keep_both_receipts(self):
        """Patching title must not drop the receipt description earned earlier.

        A receipt records how a field's text was authored. Lose one and the
        field's stored text no longer has a record of its bindings, so
        rehydration has nothing correct to consult.
        """
        self.assertEqual(self._patch({"description": "Some description"}).status_code, 200)
        self.assertEqual(self._patch({"title": "New title"}).status_code, 200)

        self.task.refresh_from_db()
        self.assertIn("description", self.task.pii_receipts)
        self.assertIn("title", self.task.pii_receipts)

    def test_a_concurrent_patch_receipt_is_not_clobbered(self):
        """The real interleaving: A reads, B commits, A writes.

        A touches only ``title``. If A's receipt payload were seeded from the
        snapshot it read BEFORE B committed, A's write would restore that stale
        map wholesale and erase B's ``description`` receipt — leaving B's
        description TEXT in place with A's stale receipt beside it. The two then
        disagree, and rehydration resolves the wrong binding. A must merge its
        one authored field onto the row as B left it.
        """
        task_id = self.task.id
        b_receipt = {"state": "placeholder", "mapping": {"[PERSON_9]": "written-by-b"}}

        def commit_b_mid_request(*args, **kwargs):
            # Runs after A's existence check and before A opens its transaction.
            Task.objects.filter(id=task_id).update(
                description="[PERSON_9] handles the renewal",
                pii_receipts={"description": b_receipt},
            )
            return ({"title": "Written by A"}, {"title": {"state": "bypass"}})

        with patch(
            "apps.integrations.runtime_views._author_runtime_lifecycle_input",
            side_effect=commit_b_mid_request,
        ):
            resp = self._patch({"title": "Written by A"})

        self.assertEqual(resp.status_code, 200)
        self.task.refresh_from_db()
        # A's own field landed.
        self.assertEqual(self.task.title, "Written by A")
        self.assertEqual(self.task.pii_receipts["title"], {"state": "bypass"})
        # B's field — text AND receipt — survived intact and still agree.
        self.assertEqual(self.task.description, "[PERSON_9] handles the renewal")
        self.assertEqual(self.task.pii_receipts["description"], b_receipt)
