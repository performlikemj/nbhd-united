from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.journal.models import Task
from apps.pii.authoring import AuthoredText
from apps.pii.repair_sweep import _check_rate_alert, repair_tenant
from apps.tenants.models import Tenant, User


class RepairSweepTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="repair", password="x")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=True,
        )

    def test_repairs_unconfirmed_row_and_becomes_idempotently_ineligible(self):
        task = Task.objects.create(
            tenant=self.tenant,
            title="Alice task",
            pii_receipts={"title": {"state": "unconfirmed", "reason": "redaction-error"}},
        )
        repaired = AuthoredText(
            text="[PERSON_1] task",
            receipt={
                "state": "placeholder",
                "redactions": [{"placeholder": "[PERSON_1]", "value": "Alice"}],
            },
        )
        with patch("apps.pii.repair_sweep.author_text", return_value=repaired) as author:
            first = repair_tenant(self.tenant, alert=False)
            second = repair_tenant(self.tenant, alert=False)

        task.refresh_from_db()
        self.assertEqual(task.title, "[PERSON_1] task")
        self.assertEqual(task.pii_receipts["title"]["state"], "placeholder")
        self.assertEqual(first["fields_repaired"], 1)
        self.assertEqual(second["fields_attempted"], 0)
        author.assert_called_once()

    def test_synthetic_error_rate_above_one_percent_fires_metadata_only_alert(self):
        with patch("apps.transcripts.alerts._send_alert", return_value=True) as send_alert:
            fired = _check_rate_alert(self.tenant, attempts=100, count=2, kind="error")

        self.assertTrue(fired)
        kwargs = send_alert.call_args.kwargs
        self.assertIn("above 1%", kwargs["subject"])
        self.assertIn(f"Tenant ID: {self.tenant.id}", kwargs["body"])
        self.assertIn("Attempts: 100", kwargs["body"])
        self.assertNotIn("Alice", kwargs["body"])

    def test_qstash_task_map_registers_repair_entrypoint(self):
        from apps.cron.views import TASK_MAP

        self.assertEqual(
            TASK_MAP["placeholder_repair_sweep"],
            "apps.pii.repair_sweep.placeholder_repair_sweep_task",
        )
