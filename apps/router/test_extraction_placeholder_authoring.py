from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.journal.models import Goal, PendingExtraction, Task
from apps.router.extraction_callbacks import _approve_goal, _approve_task
from apps.tenants.models import Tenant, User


class ExtractionPlaceholderAuthoringTests(TestCase):
    def _pending(
        self,
        *,
        enabled: bool,
        kind: str = PendingExtraction.Kind.TASK,
        text: str = "Call Alice",
        name: str = "Alice",
    ) -> PendingExtraction:
        user = User.objects.create_user(
            username=f"extract-{enabled}-{kind}-{PendingExtraction.objects.count()}",
            password="x",
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            experimental_typed_journal_lifecycle=True,
            layer1_placeholder_writes=enabled,
            pii_entity_map={"[PERSON_1]": {"name": name}},
        )
        return PendingExtraction.objects.create(
            tenant=tenant,
            kind=kind,
            text=text,
            expires_at=timezone.now() + timedelta(days=1),
        )

    def test_flag_off_is_passthrough_with_bypass_receipt(self):
        pending = self._pending(enabled=False)
        _approve_task(pending)
        task = Task.objects.get(tenant=pending.tenant)
        self.assertEqual(task.title, "Call Alice")
        self.assertEqual(task.pii_receipts["title"], {"state": "bypass"})
        self.assertEqual(task.pii_receipts["description"], {"state": "bypass"})

    def test_flag_on_stores_placeholder_and_receipt(self):
        pending = self._pending(enabled=True)
        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.authoring._detect_pii", return_value=[]),
        ):
            _approve_task(pending)

        task = Task.objects.get(tenant=pending.tenant)
        self.assertEqual(task.title, "Call [PERSON_1]")
        self.assertEqual(task.pii_receipts["title"]["state"], "placeholder")
        self.assertEqual(task.pii_receipts["description"]["state"], "placeholder")

    def test_near_limit_task_and_goal_titles_truncate_after_authoring_without_partial_token(self):
        raw_title = "x" * 250 + " Amy!"
        cases = (
            (PendingExtraction.Kind.TASK, _approve_task, Task),
            (PendingExtraction.Kind.GOAL, _approve_goal, Goal),
        )
        for kind, approve, model in cases:
            with self.subTest(kind=kind):
                pending = self._pending(
                    enabled=True,
                    kind=kind,
                    text=raw_title,
                    name="Amy",
                )
                with (
                    patch("apps.pii.redactor._detect_pii", return_value=[]),
                    patch("apps.pii.authoring._detect_pii", return_value=[]),
                ):
                    approve(pending)

                row = model.objects.get(tenant=pending.tenant)
                self.assertEqual(row.title, "x" * 250 + " ")
                self.assertLessEqual(len(row.title), model._meta.get_field("title").max_length)
                self.assertNotIn("[PERSON", row.title)
