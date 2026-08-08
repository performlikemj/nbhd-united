"""P3 W3b real automation-run payload seams."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import time, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant

from .models import Automation, AutomationRun
from .serializers import AutomationRunSerializer
from .services import execute_automation


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


class AutomationLongTailPlaceholderTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="W3b Automations", telegram_chat_id=880315)
        self.tenant.container_fqdn = "runtime.example.test"
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["container_fqdn", "pii_entity_map"])
        self.automation = Automation.objects.create(
            tenant=self.tenant,
            kind=Automation.Kind.DAILY_BRIEF,
            status=Automation.Status.ACTIVE,
            timezone="UTC",
            schedule_type=Automation.ScheduleType.DAILY,
            schedule_time=time(9, 0),
            schedule_days=[],
            next_run_at=timezone.now() + timedelta(hours=1),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _enable_placeholder_writes(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

    @staticmethod
    def _dispatch_result():
        return (
            {"message": {"text": "Ask Alice for today's review"}},
            {"status": "ok", "summary": "Alice completed the review"},
        )

    def test_flag_off_execute_preserves_payload_bytes(self):
        synthetic_update, dispatch_result = self._dispatch_result()
        with patch("apps.automations.services._dispatch_to_openclaw", return_value=(synthetic_update, dispatch_result)):
            run = execute_automation(
                automation=self.automation,
                trigger_source=AutomationRun.TriggerSource.MANUAL,
            )

        run.refresh_from_db()
        self.assertEqual(run.input_payload["synthetic_update"], synthetic_update)
        self.assertEqual(run.result_payload, {"router_response": dispatch_result})
        self.assertEqual(run.pii_receipts["input_payload"], {"state": "bypass", "writer": "background"})
        self.assertEqual(run.pii_receipts["result_payload"], {"state": "bypass", "writer": "background"})

    def test_flag_on_execute_stores_placeholders_and_owner_list_rehydrates_receipts(self):
        self._enable_placeholder_writes()
        synthetic_update, dispatch_result = self._dispatch_result()
        with (
            _checked_detection(),
            patch("apps.automations.services._dispatch_to_openclaw", return_value=(synthetic_update, dispatch_result)),
        ):
            run = execute_automation(
                automation=self.automation,
                trigger_source=AutomationRun.TriggerSource.MANUAL,
            )

        run.refresh_from_db()
        self.assertEqual(run.input_payload["synthetic_update"]["message"]["text"], "Ask [PERSON_1] for today's review")
        self.assertEqual(
            run.result_payload["router_response"]["summary"],
            "[PERSON_1] completed the review",
        )
        self.assertEqual(run.pii_receipts["input_payload"]["writer"], "background")
        self.assertEqual(run.pii_receipts["result_payload"]["writer"], "background")

        response = self.client.get("/api/v1/automations/runs/")
        self.assertEqual(response.status_code, 200)
        represented = response.data["results"][0]
        self.assertEqual(
            represented["input_payload"]["synthetic_update"]["message"]["text"],
            "Ask Alice for today's review",
        )
        self.assertEqual(
            represented["result_payload"]["router_response"]["summary"],
            "Alice completed the review",
        )
        self.assertEqual(
            represented["pii_receipts"]["result_payload"]["redactions"][0]["value"],
            "Alice",
        )

    def test_run_receipts_are_read_only(self):
        self.assertTrue(AutomationRunSerializer().fields["pii_receipts"].read_only)
