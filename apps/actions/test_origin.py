from __future__ import annotations

import hashlib
import hmac
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.cron.models import CronJob
from apps.datebook.test_b2a import DatebookB2aMixin, _event_payload
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .models import ActionType, PendingAction
from .origin import OriginStamp, verify_origin_stamp


@override_settings(NBHD_INTERNAL_API_KEY="origin-secret")
class OriginStampTests(TestCase):
    now = 1_800_000_000

    def setUp(self):
        self.tenant = seed_internal_key(create_tenant(display_name="Origin", telegram_chat_id=77101))
        self.tenant.model_tier = "premium"
        self.tenant.save(update_fields=["model_tier"])
        self.other = seed_internal_key(create_tenant(display_name="Other", telegram_chat_id=77102))
        CronJob.objects.create(
            tenant=self.tenant,
            name="Morning Briefing",
            gateway_job_id="job-1",
        )

    def _origin(self, **changes):
        data = {
            "v": 1,
            "kind": "cron",
            "tenant_id": str(self.tenant.id),
            "run_id": "run-1",
            "job_id": "job-1",
            "ts": self.now,
        }
        data.update(changes)
        message = f"nbhd-origin.v1|{data['tenant_id']}|{data['kind']}|{data['run_id']}|{data['job_id']}|{data['ts']}"
        data["sig"] = hmac.new(
            self.tenant.internal_api_key.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        if "sig" in changes:
            data["sig"] = changes["sig"]
        return data

    @patch("apps.actions.origin.time.time", return_value=now)
    def test_valid_stamp_resolves_server_side_cron_name(self, _time):
        self.assertEqual(
            verify_origin_stamp(self.tenant, self._origin()),
            OriginStamp(kind="cron", run_id="run-1", job_id="job-1", cron_name="Morning Briefing"),
        )

    @patch("apps.actions.origin.time.time", return_value=now)
    def test_invalid_stamp_matrix_is_unknown_and_never_user(self, _time):
        cases = {
            "absent": None,
            "malformed": {"v": 1},
            "bad_sig": self._origin(sig="0" * 64),
            "stale": self._origin(ts=self.now - 901),
            "wrong_tenant": self._origin(tenant_id=str(self.other.id)),
            "unknown_kind": self._origin(kind="user"),
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                stamp = verify_origin_stamp(self.tenant, value)
                self.assertEqual(stamp, OriginStamp())
                self.assertNotEqual(stamp.kind, "user")

    def test_generic_gate_records_verified_origin_and_ignores_origin_kind_field(self):
        # GateRequest imports the sender locally; use an app-free tenant so the
        # request reaches its established undeliverable response after insert.
        valid = self._origin()
        with (
            patch("apps.actions.messaging.send_gate_confirmation", return_value=True),
            patch("apps.actions.origin.time.time", return_value=self.now),
        ):
            response = self.client.post(
                f"/api/v1/internal/runtime/{self.tenant.id}/gate/request/",
                data={
                    "action_type": ActionType.GMAIL_DELETE,
                    "payload": {"message_id": "m1"},
                    "display_summary": "Delete message",
                    "origin": valid,
                    "origin_kind": "user",
                },
                content_type="application/json",
                HTTP_X_INTERNAL_KEY=self.tenant.internal_api_key,
                HTTP_X_TENANT_ID=str(self.tenant.id),
            )
        self.assertEqual(response.status_code, 202, response.content)
        action = PendingAction.objects.get(pk=response.json()["action_id"])
        self.assertEqual(action.origin_kind, "cron")
        self.assertEqual(action.origin_cron_name, "Morning Briefing")
        self.assertEqual(action.origin_run_id, "run-1")

        with patch("apps.actions.messaging.send_gate_confirmation", return_value=True):
            response = self.client.post(
                f"/api/v1/internal/runtime/{self.tenant.id}/gate/request/",
                data={
                    "action_type": ActionType.GMAIL_DELETE,
                    "payload": {"message_id": "m2"},
                    "display_summary": "Delete another message",
                    "origin_kind": "cron",
                },
                content_type="application/json",
                HTTP_X_INTERNAL_KEY=self.tenant.internal_api_key,
                HTTP_X_TENANT_ID=str(self.tenant.id),
            )
        action = PendingAction.objects.get(pk=response.json()["action_id"])
        self.assertEqual(action.origin_kind, "unknown")


class DatebookOriginRecordingTests(DatebookB2aMixin, TestCase):
    chat_id = 77103

    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    @patch("apps.actions.origin.time.time", return_value=1_800_000_000)
    def test_runtime_body_origin_is_verified_and_recorded(self, _time, _send):
        CronJob.objects.create(
            tenant=self.tenant,
            name="Week Ahead",
            gateway_job_id="datebook-job",
        )
        message = f"nbhd-origin.v1|{self.tenant.id}|cron|datebook-run|datebook-job|1800000000"
        origin = {
            "v": 1,
            "kind": "cron",
            "tenant_id": str(self.tenant.id),
            "run_id": "datebook-run",
            "job_id": "datebook-job",
            "ts": 1_800_000_000,
            "sig": hmac.new(
                self.tenant.internal_api_key.encode(),
                message.encode(),
                hashlib.sha256,
            ).hexdigest(),
        }

        response = self.client.post(
            f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create",
            {
                "request_id": "origin-datebook",
                "command_type": "calendar_create",
                "payload": _event_payload(),
                "direct_user_originated": False,
                "origin": origin,
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 202, response.data)
        action = PendingAction.objects.get(datebook_request_id="origin-datebook")
        self.assertEqual(action.origin_kind, "cron")
        self.assertEqual(action.origin_cron_name, "Week Ahead")
        self.assertEqual(action.origin_run_id, "datebook-run")
