from __future__ import annotations

import threading
from copy import deepcopy
from datetime import timedelta
from time import monotonic
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.actions.models import PendingAction

from .gate import DATEBOOK_DUPLICATE_WINDOW
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
