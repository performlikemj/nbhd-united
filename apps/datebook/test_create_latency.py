from __future__ import annotations

import threading
from copy import deepcopy
from datetime import timedelta
from time import monotonic
from unittest.mock import patch

from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.actions.models import PendingAction
from apps.tenants.models import Tenant

from .gate import DATEBOOK_CREATE_RETRY_GUIDANCE, DATEBOOK_CREATE_RETRY_STATE, DATEBOOK_DUPLICATE_WINDOW
from .models import DeviceCommand
from .test_b2a import DatebookB2aMixin, _event_payload


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class DatebookRequestCreateLatencyTests(DatebookB2aMixin, TransactionTestCase):
    chat_id = 929001
    reset_sequences = True

    @patch("apps.datebook.notify.apns_configured", return_value=True)
    @patch("apps.router.push_views._push_to_user_devices")
    def test_request_create_returns_before_hanging_push_transport(self, push, _configured):
        push_started = threading.Event()
        release_push = threading.Event()
        push_finished = threading.Event()

        def hanging_push(*_args, **_kwargs):
            push_started.set()
            release_push.wait(timeout=2)
            push_finished.set()
            return {"token_count": 1, "used_fallback": False}

        push.side_effect = hanging_push
        started_at = monotonic()
        try:
            with override_settings(NBHD_DISABLE_BACKGROUND_THREADS=False):
                response = self.client.post(
                    f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create",
                    {
                        "request_id": "slow-push-request",
                        "command_type": DeviceCommand.CommandType.CALENDAR_CREATE,
                        "payload": _event_payload(title="Push must not block"),
                        "direct_user_originated": True,
                        "originating_channel": "app",
                    },
                    format="json",
                    **self.headers,
                )
            elapsed = monotonic() - started_at

            self.assertEqual(response.status_code, 202, response.data)
            self.assertTrue(push_started.wait(timeout=0.5), "background push did not start")
            self.assertLess(elapsed, 0.2, f"request waited {elapsed:.3f}s for the push transport")
        finally:
            release_push.set()
            push_finished.wait(timeout=1)

    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    def test_request_create_lock_wait_is_bounded_and_retriable(self, _send):
        lock_acquired = threading.Event()
        release_lock = threading.Event()
        holder_errors = []

        def hold_tenant_lock():
            close_old_connections()
            try:
                with transaction.atomic():
                    Tenant.objects.select_for_update().get(pk=self.tenant.pk)
                    lock_acquired.set()
                    release_lock.wait(timeout=3)
            except Exception as exc:  # pragma: no cover - asserted below
                holder_errors.append(exc)
            finally:
                close_old_connections()

        holder = threading.Thread(target=hold_tenant_lock, daemon=True)
        holder.start()
        self.assertTrue(lock_acquired.wait(timeout=1), "lock holder did not start")
        started_at = monotonic()
        try:
            with override_settings(
                DATEBOOK_REQUEST_CREATE_LOCK_TIMEOUT_MS=100,
                DATEBOOK_REQUEST_CREATE_STATEMENT_TIMEOUT_MS=1_000,
            ):
                response = self.client.post(
                    f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create",
                    {
                        "request_id": "bounded-lock-wait",
                        "command_type": DeviceCommand.CommandType.CALENDAR_CREATE,
                        "payload": _event_payload(title="Bound the lock"),
                        "direct_user_originated": True,
                        "originating_channel": "app",
                    },
                    format="json",
                    **self.headers,
                )
            elapsed = monotonic() - started_at
        finally:
            release_lock.set()
            holder.join(timeout=1)

        self.assertFalse(holder.is_alive(), "lock holder did not exit")
        self.assertEqual(holder_errors, [])
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(
            response.data,
            {
                "state": DATEBOOK_CREATE_RETRY_STATE,
                "retriable": True,
                "created": False,
                "guidance": DATEBOOK_CREATE_RETRY_GUIDANCE,
            },
        )
        self.assertLess(elapsed, 1, f"request lock wait lasted {elapsed:.3f}s")
        self.assertFalse(PendingAction.objects.filter(datebook_request_id="bounded-lock-wait").exists())


class DatebookRequestCreateAuthoringTests(DatebookB2aMixin, TestCase):
    chat_id = 929003

    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    @patch("apps.pii.authoring._detect_pii", return_value=[])
    @patch("apps.pii.redactor._detect_pii", return_value=[])
    def test_due_alarm_create_defers_blocking_detector_off_request_path(
        self,
        redactor_detect,
        authoring_detect,
        _send,
    ):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        detector_started = threading.Event()
        detector_release = threading.Event()

        def hanging_detector(*_args, **_kwargs):
            if not detector_started.is_set():
                detector_started.set()
                detector_release.wait(timeout=0.5)
            return []

        redactor_detect.side_effect = hanging_detector
        authoring_detect.side_effect = hanging_detector
        target_at = (timezone.now() + timedelta(hours=1)).replace(microsecond=0)

        started_at = monotonic()
        response = self.client.post(
            f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create",
            {
                "request_id": "due-alarm-diagnostic",
                "command_type": DeviceCommand.CommandType.REMINDER_CREATE,
                "payload": {
                    "items": [
                        {
                            "title": "Call Alice",
                            "due": {
                                "kind": "zoned",
                                "due_at": target_at.isoformat(),
                                "tz_id": "Asia/Tokyo",
                            },
                            "alarm": {
                                "kind": "absolute",
                                "trigger_at": target_at.isoformat(),
                            },
                        }
                    ]
                },
                "direct_user_originated": True,
                "originating_channel": "app",
            },
            format="json",
            **self.headers,
        )
        elapsed = monotonic() - started_at

        self.assertEqual(response.status_code, 202, response.data)
        self.assertLess(elapsed, 0.2, f"request waited {elapsed:.3f}s for deferred detection")
        self.assertFalse(detector_started.is_set())
        redactor_detect.assert_not_called()
        authoring_detect.assert_not_called()
        action = PendingAction.objects.get(datebook_request_id="due-alarm-diagnostic")
        self.assertEqual(action.action_payload["payload"]["items"][0]["title"], "Call [PERSON_1]")
        self.assertEqual(
            action.pii_receipts["action_payload"],
            {
                "state": "unconfirmed",
                "reason": "detector-deferred",
                "redactions": [{"placeholder": "[PERSON_1]"}],
                "writer": "runtime",
            },
        )


class DatebookDuplicateRequestGuardTests(DatebookB2aMixin, TestCase):
    chat_id = 929002

    def _post(self, request_id: str, payload: dict):
        return self.client.post(
            f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create",
            {
                "request_id": request_id,
                "command_type": DeviceCommand.CommandType.CALENDAR_CREATE,
                "payload": payload,
                "direct_user_originated": True,
                "originating_channel": "app",
            },
            format="json",
            **self.headers,
        )

    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    def test_distinct_request_id_for_same_logical_ask_is_typed_duplicate(self, _send):
        first_payload = _event_payload(title="  Team   Sync  ")
        duplicate_payload = deepcopy(first_payload)
        duplicate_payload["items"][0]["title"] = "team sync"

        first = self._post("first-tool-call", first_payload)
        duplicate = self._post("retry-tool-call", duplicate_payload)

        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(duplicate.status_code, 409, duplicate.data)
        self.assertEqual(duplicate.data["state"], "duplicate_request")
        self.assertEqual(duplicate.data["existing_action_id"], first.data["action_id"])
        self.assertEqual(PendingAction.objects.filter(tenant=self.tenant).count(), 1)

    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    def test_target_minute_and_recent_window_bound_the_duplicate_guard(self, _send):
        payload = _event_payload(title="Team sync")
        first = self._post("first-minute", payload)
        shifted = deepcopy(payload)
        for key in ("start_at", "end_at"):
            parsed = parse_datetime(shifted["items"][0]["time"][key])
            shifted["items"][0]["time"][key] = (parsed + timedelta(minutes=1)).isoformat()

        next_minute = self._post("next-minute", shifted)
        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(next_minute.status_code, 202, next_minute.data)

        PendingAction.objects.filter(pk=first.data["action_id"]).update(
            created_at=timezone.now() - DATEBOOK_DUPLICATE_WINDOW - timedelta(seconds=1)
        )
        outside_window = self._post("outside-window", payload)
        self.assertEqual(outside_window.status_code, 202, outside_window.data)
