from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.steward.collectors import openrouter
from apps.steward.digest import render_steward_daily_digest
from apps.steward.models import (
    CollectorStatus,
    EvidenceEvent,
    EvidenceSource,
    OpenRouterModelDaily,
)
from apps.tenants.models import Tenant

MGMT_KEY = "or-mgmt-FAKE-TEST-ONLY"
DYNAMIC_MODEL = "example-provider/dynamically-discovered-model"
CANARY_HASH = "raw-key-hash-test-only"


class FakeResponse:
    def __init__(self, payload, *, status_error: Exception | None = None):
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error is not None:
            raise self.status_error

    def json(self):
        return self.payload


def analytics_payload(rows):
    return {
        "data": {
            "data": rows,
            "metadata": {
                "query_time_ms": 3,
                "row_count": len(rows),
                "truncated": False,
            },
        }
    }


class OpenRouterCollectorTests(TestCase):
    def _client(self):
        client = MagicMock()

        def post(url, json):
            self.assertEqual(url, "https://openrouter.ai/api/v1/analytics/query")
            if json["dimensions"] == ["provider", "finish_reason"]:
                return FakeResponse(
                    analytics_payload(
                        [
                            {
                                "provider": "Example Provider",
                                "finish_reason": "stop",
                                "request_count": "12",
                                "avg_latency": 245.5,
                            }
                        ]
                    )
                )
            if "filters" in json:
                return FakeResponse(
                    analytics_payload(
                        [
                            {
                                "model": DYNAMIC_MODEL,
                                "finish_reason": "tool_calls",
                                "request_count": "7",
                                "avg_latency": "123.25",
                            }
                        ]
                    )
                )
            return FakeResponse(
                analytics_payload(
                    [
                        {
                            "model": DYNAMIC_MODEL,
                            "finish_reason": "tool_calls",
                            "request_count": "9",
                            "avg_latency": 120,
                        },
                        {
                            "model": DYNAMIC_MODEL,
                            "finish_reason": None,
                            "request_count": 1,
                            "avg_latency": None,
                        },
                    ]
                )
            )

        client.post.side_effect = post
        context = MagicMock()
        context.__enter__.return_value = client
        context.__exit__.return_value = False
        return context, client

    @override_settings(STEWARD_OPENROUTER_MGMT_KEY="")
    @patch("apps.steward.collectors.openrouter.httpx.Client")
    def test_key_unset_noops_and_stamps_not_configured(self, client_class):
        self.assertEqual(openrouter.collect_openrouter(), {"queries": 0, "rows": 0, "evidence": 0})
        self.assertEqual(openrouter.collect_openrouter(), {"queries": 0, "rows": 0, "evidence": 0})

        client_class.assert_not_called()
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.OPENROUTER)
        self.assertEqual(status.last_error_class, "not_configured")
        self.assertEqual(status.detail, "STEWARD_OPENROUTER_MGMT_KEY unset")
        self.assertEqual(status.consecutive_failures, 2)
        self.assertIsNone(status.last_success_at)
        self.assertIsNone(status.held_until)

    @override_settings(STEWARD_OPENROUTER_MGMT_KEY=MGMT_KEY)
    def test_happy_path_discovers_models_uses_hash_filter_and_stamps_clean_success(self):
        user = get_user_model().objects.create_user(
            username="openrouter-canary",
            display_name="CANARY DISPLAY NAME MUST NOT BE A FILTER",
        )
        tenant = Tenant.objects.create(user=user, openrouter_key_hash=CANARY_HASH)
        attempted_at = datetime(2026, 8, 4, 0, 25, tzinfo=UTC)
        CollectorStatus.objects.create(
            collector=CollectorStatus.Collector.OPENROUTER,
            consecutive_failures=4,
            last_error_class="previous_failure",
        )
        context, client = self._client()

        with (
            override_settings(STEWARD_OPENROUTER_CANARY_TENANT_ID=str(tenant.id)),
            patch("apps.steward.collectors.openrouter.timezone.now", return_value=attempted_at),
            patch("apps.steward.collectors.openrouter.httpx.Client", return_value=context) as client_class,
        ):
            result = openrouter.collect_openrouter()

        self.assertEqual(result, {"queries": 3, "rows": 4, "evidence": 0})
        self.assertEqual(OpenRouterModelDaily.objects.count(), 4)
        self.assertEqual(
            set(OpenRouterModelDaily.objects.values_list("model", flat=True)),
            {DYNAMIC_MODEL, "Example Provider"},
        )
        null_row = OpenRouterModelDaily.objects.get(
            scope=OpenRouterModelDaily.Scope.ACCOUNT,
            finish_reason="null",
        )
        self.assertEqual(null_row.request_count, 1)
        self.assertIsNone(null_row.avg_latency_ms)

        calls = client.post.call_args_list
        self.assertEqual(
            [call.kwargs["json"]["dimensions"] for call in calls],
            [
                ["model", "finish_reason"],
                ["model", "finish_reason"],
                ["provider", "finish_reason"],
            ],
        )
        for call in calls:
            body = call.kwargs["json"]
            self.assertEqual(body["metrics"], ["request_count", "avg_latency"])
            self.assertEqual(
                body["time_range"],
                {
                    "start": "2026-08-03T00:00:00Z",
                    "end": "2026-08-04T00:00:00Z",
                },
            )
        self.assertNotIn("filters", calls[0].kwargs["json"])
        self.assertEqual(
            calls[1].kwargs["json"]["filters"],
            [{"field": "api_key_id", "operator": "eq", "value": CANARY_HASH}],
        )
        self.assertNotIn(user.display_name, str(calls[1].kwargs["json"]))
        self.assertNotIn("filters", calls[2].kwargs["json"])
        self.assertEqual(
            client_class.call_args.kwargs["headers"]["Authorization"],
            f"Bearer {MGMT_KEY}",
        )

        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.OPENROUTER)
        self.assertEqual(status.last_success_at, attempted_at)
        self.assertEqual(status.last_attempt_at, attempted_at)
        self.assertEqual(status.last_error_class, "")
        self.assertEqual(status.consecutive_failures, 0)
        self.assertIn("canary=collected", status.detail)
        self.assertIsNone(status.held_until)

    @override_settings(
        STEWARD_OPENROUTER_MGMT_KEY=MGMT_KEY,
        STEWARD_OPENROUTER_CANARY_TENANT_ID="",
    )
    def test_rerun_upserts_without_duplicates(self):
        attempted_at = datetime(2026, 8, 4, 0, 25, tzinfo=UTC)
        first_context, _ = self._client()
        second_context, _ = self._client()
        with (
            patch("apps.steward.collectors.openrouter.timezone.now", return_value=attempted_at),
            patch(
                "apps.steward.collectors.openrouter.httpx.Client",
                side_effect=[first_context, second_context],
            ),
        ):
            first = openrouter.collect_openrouter()
            second = openrouter.collect_openrouter()

        self.assertEqual(first, {"queries": 2, "rows": 3, "evidence": 0})
        self.assertEqual(second, first)
        self.assertEqual(OpenRouterModelDaily.objects.count(), 3)
        self.assertEqual(EvidenceEvent.objects.count(), 0)

    @override_settings(
        STEWARD_OPENROUTER_MGMT_KEY=MGMT_KEY,
        STEWARD_OPENROUTER_CANARY_TENANT_ID="",
    )
    def test_http_failure_is_stamped_and_does_not_escape(self):
        client = MagicMock()
        client.post.side_effect = httpx.ConnectError("test connection failure")
        context = MagicMock()
        context.__enter__.return_value = client
        context.__exit__.return_value = False

        with patch("apps.steward.collectors.openrouter.httpx.Client", return_value=context):
            result = openrouter.collect_openrouter()

        self.assertEqual(result, {"queries": 0, "rows": 0, "evidence": 0})
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.OPENROUTER)
        self.assertEqual(status.last_error_class, "ConnectError")
        self.assertEqual(status.detail, "OpenRouter analytics collection failed")
        self.assertEqual(status.consecutive_failures, 1)
        self.assertIsNone(status.held_until)

    @override_settings(
        STEWARD_OPENROUTER_MGMT_KEY=MGMT_KEY,
        STEWARD_OPENROUTER_CANARY_TENANT_ID="",
    )
    def test_shape_error_is_stamped_without_persisting_partial_rows(self):
        client = MagicMock()
        client.post.return_value = FakeResponse({"data": {"data": [], "metadata": {}}})
        context = MagicMock()
        context.__enter__.return_value = client
        context.__exit__.return_value = False

        with patch("apps.steward.collectors.openrouter.httpx.Client", return_value=context):
            result = openrouter.collect_openrouter()

        self.assertEqual(result["rows"], 0)
        self.assertFalse(OpenRouterModelDaily.objects.exists())
        status = CollectorStatus.objects.get(collector=CollectorStatus.Collector.OPENROUTER)
        self.assertEqual(status.last_error_class, "OpenRouterAnalyticsError")


class OpenRouterDriftTests(TestCase):
    def _day(
        self,
        day: date,
        *,
        scope=OpenRouterModelDaily.Scope.ACCOUNT,
        model=DYNAMIC_MODEL,
        tool_calls=0,
        null=0,
        stop=0,
    ):
        rows = []
        for finish_reason, count in (
            ("tool_calls", tool_calls),
            ("null", null),
            ("stop", stop),
        ):
            if count:
                rows.append(
                    OpenRouterModelDaily(
                        date=day,
                        scope=scope,
                        model=model,
                        finish_reason=finish_reason,
                        request_count=count,
                    )
                )
        OpenRouterModelDaily.objects.bulk_create(rows)

    def test_share_drop_and_null_rate_math_fire_after_three_history_days(self):
        target = date(2026, 8, 3)
        for days_ago in (1, 2, 3):
            self._day(target - timedelta(days=days_ago), tool_calls=50, stop=50)
        self._day(target, tool_calls=30, null=1, stop=69)

        inputs = openrouter.drift_inputs(
            target_date=target,
            collected_at=datetime(2026, 8, 4, tzinfo=UTC),
        )
        by_kind = {item.payload["kind"]: item.payload for item in inputs}

        self.assertEqual(set(by_kind), {"tool_calls_share_drop", "null_rate"})
        self.assertEqual(by_kind["tool_calls_share_drop"]["baseline_pct"], 50.0)
        self.assertEqual(by_kind["tool_calls_share_drop"]["current_pct"], 30.0)
        self.assertEqual(by_kind["tool_calls_share_drop"]["drop_pts"], 20.0)
        self.assertFalse(by_kind["tool_calls_share_drop"]["severe"])
        self.assertEqual(by_kind["null_rate"]["current_pct"], 1.0)
        self.assertFalse(by_kind["null_rate"]["severe"])

    def test_insufficient_history_does_not_fire(self):
        target = date(2026, 8, 3)
        for days_ago in (1, 2):
            self._day(target - timedelta(days=days_ago), tool_calls=80, stop=20)
        self._day(target, null=100)

        self.assertEqual(
            openrouter.drift_inputs(
                target_date=target,
                collected_at=datetime(2026, 8, 4, tzinfo=UTC),
            ),
            [],
        )

    def test_new_provider_fires_against_three_day_provider_history(self):
        target = date(2026, 8, 3)
        for days_ago in (1, 2, 3):
            self._day(
                target - timedelta(days=days_ago),
                scope=OpenRouterModelDaily.Scope.PROVIDER,
                model="Existing Provider",
                stop=10,
            )
        self._day(
            target,
            scope=OpenRouterModelDaily.Scope.PROVIDER,
            model="Existing Provider",
            stop=10,
        )
        self._day(
            target,
            scope=OpenRouterModelDaily.Scope.PROVIDER,
            model="New Provider",
            stop=5,
        )

        inputs = openrouter.drift_inputs(
            target_date=target,
            collected_at=datetime(2026, 8, 4, tzinfo=UTC),
        )

        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0].payload["kind"], "new_provider")
        self.assertEqual(inputs[0].payload["provider"], "New Provider")

    def test_drift_event_renders_as_code_generated_digest_fact(self):
        now = datetime(2026, 8, 4, 1, tzinfo=UTC)
        EvidenceEvent.objects.create(
            source=EvidenceSource.OPENROUTER_MODEL_HEALTH,
            subject="openrouter-health:test",
            occurred_at=now - timedelta(hours=1),
            received_at=now - timedelta(minutes=1),
            payload={
                "kind": "tool_calls_share_drop",
                "date": "2026-08-03",
                "scope": "account",
                "model": DYNAMIC_MODEL,
                "current_pct": 30.0,
                "baseline_pct": 50.0,
                "drop_pts": 20.0,
                "baseline_days": 3,
                "severe": False,
                "poison": "NEVER_RENDER_THIS",
            },
            fingerprint="openrouter_model_health:test-digest-event",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.COLLECTOR,
        )

        text, stats = render_steward_daily_digest(now=now)

        self.assertIn("OPENROUTER", text)
        self.assertIn("tool_calls 30.00% vs 50.00%", text)
        self.assertIn(DYNAMIC_MODEL, text)
        self.assertNotIn("NEVER_RENDER_THIS", text)
        self.assertEqual(stats["openrouter"], 1)
