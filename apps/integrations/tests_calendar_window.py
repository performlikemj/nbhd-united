"""Tests for ``_resolve_calendar_window`` and ``_build_window``.

Calendar runtime endpoints accept an optional ``window_kind`` /
``window_value`` pair that resolves to RFC3339 ``time_min`` / ``time_max``
in the tenant's tz. This module pins that behavior so the agent can stop
computing dates LLM-side.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase
from rest_framework.response import Response

from apps.integrations.runtime_views import _build_window, _resolve_calendar_window


def _fake_tenant(tz_name: str = "America/Los_Angeles") -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(timezone=tz_name))


class BuildWindowTests(TestCase):
    def test_zero_value_kind(self):
        w = _build_window("today", None)
        self.assertEqual(w.kind, "today")

    def test_int_value_kind(self):
        w = _build_window("last_n_days", "7")
        self.assertEqual(w.kind, "last_n_days")
        self.assertEqual(w.value, 7)

    def test_int_kind_requires_value(self):
        with self.assertRaises(ValueError):
            _build_window("last_n_days", None)

    def test_since_requires_date(self):
        w = _build_window("since", "2026-04-01")
        self.assertEqual(w.kind, "since")
        self.assertEqual(w.value, date(2026, 4, 1))

    def test_between_csv(self):
        w = _build_window("between", "2026-04-01,2026-04-15")
        self.assertEqual(w.kind, "between")
        self.assertEqual(w.value, [date(2026, 4, 1), date(2026, 4, 15)])

    def test_between_rejects_single_date(self):
        with self.assertRaises(ValueError):
            _build_window("between", "2026-04-01")

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            _build_window("forever", None)


class ResolveCalendarWindowTests(SimpleTestCase):
    """End-to-end exercises of the resolver against a mocked request."""

    def setUp(self):
        self.factory = RequestFactory()
        # Patch django.utils.timezone.now via the path runtime_views uses
        # so resolve_window's "today" is deterministic.
        self.frozen_now = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
        self.patcher = patch("apps.common.windows.datetime")
        mock_dt = self.patcher.start()
        # Pass-through everything except .now()
        mock_dt.now.return_value = self.frozen_now
        mock_dt.combine = datetime.combine
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.min = datetime.min
        mock_dt.max = datetime.max
        self.addCleanup(self.patcher.stop)

    def _drf_request(self, query_string: str):
        django_request = self.factory.get(f"/runtime/?{query_string}")
        from rest_framework.request import Request

        return Request(django_request)

    def test_no_params_returns_none_pair(self):
        result = _resolve_calendar_window(self._drf_request(""), _fake_tenant())
        self.assertEqual(result, (None, None))

    def test_legacy_time_min_passes_through(self):
        result = _resolve_calendar_window(
            self._drf_request("time_min=2026-05-20T00:00:00Z&time_max=2026-05-21T00:00:00Z"),
            _fake_tenant(),
        )
        self.assertEqual(result, ("2026-05-20T00:00:00Z", "2026-05-21T00:00:00Z"))

    def test_window_kind_resolves_to_tenant_tz_iso(self):
        # 2026-05-20 18:00 UTC = 2026-05-20 11:00 PDT, so "today" in LA = 2026-05-20.
        result = _resolve_calendar_window(
            self._drf_request("window_kind=today"),
            _fake_tenant("America/Los_Angeles"),
        )
        self.assertIsInstance(result, tuple)
        time_min, time_max = result
        self.assertTrue(time_min.startswith("2026-05-20T00:00:00"))
        self.assertTrue(time_max.startswith("2026-05-20T23:59:59"))
        # Both carry the LA offset (-07:00 in May)
        self.assertIn("-07:00", time_min)
        self.assertIn("-07:00", time_max)

    def test_window_kind_with_value(self):
        result = _resolve_calendar_window(
            self._drf_request("window_kind=last_n_days&window_value=3"),
            _fake_tenant("America/Los_Angeles"),
        )
        time_min, time_max = result
        # last_n_days(3) on 2026-05-20 → (2026-05-18, 2026-05-20)
        self.assertTrue(time_min.startswith("2026-05-18T00:00:00"))
        self.assertTrue(time_max.startswith("2026-05-20T23:59:59"))

    def test_window_and_legacy_combined_returns_400(self):
        result = _resolve_calendar_window(
            self._drf_request("window_kind=today&time_min=2026-05-20T00:00:00Z"),
            _fake_tenant(),
        )
        self.assertIsInstance(result, Response)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.data["error"], "invalid_request")

    def test_invalid_window_kind_returns_400(self):
        result = _resolve_calendar_window(
            self._drf_request("window_kind=forever"),
            _fake_tenant(),
        )
        self.assertIsInstance(result, Response)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.data["error"], "invalid_window")

    def test_missing_value_for_int_kind_returns_400(self):
        result = _resolve_calendar_window(
            self._drf_request("window_kind=last_n_days"),
            _fake_tenant(),
        )
        self.assertIsInstance(result, Response)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.data["error"], "invalid_window")

    def test_all_kind_returns_open_range(self):
        result = _resolve_calendar_window(
            self._drf_request("window_kind=all"),
            _fake_tenant(),
        )
        self.assertEqual(result, (None, None))
