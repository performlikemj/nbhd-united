"""Tests for the content-free telemetry emitter.

The invariant these pin: a caller CANNOT get free text into a tool event, whether
they try via an undeclared key, a long string, a nested structure, or prose under
a key that is legitimately allowlisted.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.test import TestCase

from .models import ToolContractEvent
from .telemetry import MAX_DETAIL_KEYS, MAX_STRING_LEN, emit_tool_event


class EmitToolEventTests(TestCase):
    def test_records_the_basic_shape(self):
        event = emit_tool_event(
            namespace="fuel",
            tool_name="runtime-fuel-log",
            tenant_id=uuid.uuid4(),
            outcome="accepted",
            duration_ms=42,
        )
        self.assertIsNotNone(event)
        stored = ToolContractEvent.objects.get(id=event.id)
        self.assertEqual(stored.tool_name, "runtime-fuel-log")
        self.assertEqual(stored.outcome, "accepted")
        self.assertEqual(stored.duration_ms, 42)
        self.assertEqual(stored.detail, {})

    def test_allowlisted_flags_survive(self):
        event = emit_tool_event(
            namespace="fuel",
            tool_name="runtime-fuel-log",
            outcome="normalized",
            reason_code="weekday_coerced",
            detail={"weekday_key_style": "name", "start_today_reject": False, "status": 200},
        )
        self.assertEqual(
            event.detail,
            {"weekday_key_style": "name", "start_today_reject": False, "status": 200},
        )
        self.assertEqual(event.reason_code, "weekday_coerced")

    # --- the smuggle attempts -------------------------------------------------

    def test_free_text_smuggle_is_structurally_blocked(self):
        """A non-allowlisted key and an over-long string both fail to get through."""
        long_code = "a" * 200
        event = emit_tool_event(
            namespace="fuel",
            tool_name="runtime-fuel-log",
            outcome="rejected",
            detail={
                # Not in the fuel allowlist — must not be stored at all.
                "user_note": "Dinner with Jay at the ramen place on Tuesday",
                # Allowlisted key, but the value is prose — must not be stored.
                "weekday_key_style": "the user said next Tuesday evening",
                # Allowlisted key, code-shaped, but far too long — capped.
                "date_source": long_code,
            },
        )

        self.assertNotIn("user_note", event.detail)
        blob = repr(event.detail)
        self.assertNotIn("Jay", blob)
        self.assertNotIn("ramen", blob)
        self.assertNotIn("the user said", blob)

        # The long value survives, but only its capped head.
        self.assertEqual(event.detail["date_source"], "a" * MAX_STRING_LEN)
        self.assertEqual(len(event.detail["date_source"]), MAX_STRING_LEN)

        # Both losses are counted rather than silent.
        self.assertEqual(event.detail["dropped_keys"], 2)
        self.assertEqual(event.detail["truncated_keys"], 1)

    def test_nested_structures_are_dropped(self):
        event = emit_tool_event(
            namespace="fuel",
            tool_name="runtime-fuel-log",
            outcome="rejected",
            detail={
                "date_source": {"raw": "next Tuesday with Jay"},
                "weekday_key_style": ["mon", "a whole sentence of notes"],
            },
        )
        self.assertEqual(event.detail, {"dropped_keys": 2})
        self.assertNotIn("Jay", repr(event.detail))

    def test_unregistered_namespace_gets_common_keys_only(self):
        event = emit_tool_event(
            namespace="brandnew",
            tool_name="some-tool",
            outcome="accepted",
            detail={"status": 200, "weekday_key_style": "name"},
        )
        self.assertEqual(event.detail, {"status": 200, "dropped_keys": 1})

    def test_detail_key_count_is_bounded(self):
        detail = {"status": 200, "method": "POST", "app": "fuel"}
        detail.update({f"extra_{i}": i for i in range(MAX_DETAIL_KEYS + 10)})
        event = emit_tool_event(namespace="fuel", tool_name="t", outcome="accepted", detail=detail)
        self.assertLessEqual(len(event.detail), MAX_DETAIL_KEYS + 2)  # + dropped/truncated counters

    def test_prose_tool_name_is_refused(self):
        event = emit_tool_event(tool_name="log the workout for Jay", outcome="error")
        self.assertEqual(event.tool_name, "invalid_tool_name")
        self.assertNotIn("Jay", event.tool_name)

    def test_prose_reason_code_is_refused(self):
        event = emit_tool_event(
            tool_name="runtime-fuel-log",
            outcome="rejected",
            reason_code="user asked for Tuesday but meant Wednesday",
        )
        self.assertEqual(event.reason_code, "")

    def test_unknown_outcome_becomes_error(self):
        event = emit_tool_event(tool_name="t", outcome="totally-made-up")
        self.assertEqual(event.outcome, ToolContractEvent.Outcome.ERROR)

    def test_unusable_tenant_id_stores_null(self):
        event = emit_tool_event(tool_name="t", outcome="accepted", tenant_id="not-a-uuid")
        self.assertIsNone(event.tenant_id)

    # --- fail-open ------------------------------------------------------------

    def test_emission_failure_does_not_propagate(self):
        """Telemetry must never break the tool call it observes."""
        with (
            patch.object(
                ToolContractEvent.objects,
                "create",
                side_effect=RuntimeError("database is on fire"),
            ),
            self.assertLogs("apps.platform_logs.telemetry", level="WARNING") as logs,
        ):
            result = emit_tool_event(tool_name="runtime-fuel-log", outcome="accepted")

        self.assertIsNone(result)
        self.assertIn("emission failed", "\n".join(logs.output))
        self.assertEqual(ToolContractEvent.objects.count(), 0)

    def test_sanitizer_failure_does_not_propagate(self):
        """Even a bug inside the sanitizer itself stays contained."""
        with (
            patch("apps.platform_logs.telemetry._sanitize_detail", side_effect=ValueError("boom")),
            self.assertLogs("apps.platform_logs.telemetry", level="WARNING"),
        ):
            result = emit_tool_event(tool_name="t", outcome="accepted", detail={"status": 200})
        self.assertIsNone(result)
