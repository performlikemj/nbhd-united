from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.steward.digest import render_steward_daily_digest
from apps.steward.facts import compose_steward_facts
from apps.steward.models import (
    AlertState,
    CollectorStatus,
    DigestRecord,
    EvidenceEvent,
    EvidenceSource,
    Expectation,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
FACTS_SNAPSHOT = {
    "version": 1,
    "generated_at": "2026-09-03T12:00:00Z",
    "since": "2026-09-02T12:00:00Z",
    "stats": {
        "needs_you": 0,
        "trains": 0,
        "stalled": 1,
        "slo_evals": 0,
        "openrouter": 0,
        "repos": 0,
        "integrity": 0,
    },
    "stalled": [
        {
            "id": "expectation:41",
            "expectation_id": 41,
            "subject": "basecamp-pm",
            "kind": "heartbeat",
            "state": "missed",
            "on_miss": "urgent",
            "due_at": "2026-09-01T12:00:00Z",
            "overdue_seconds": 172800,
            "last_alerted_at": "2026-09-03T09:00:00Z",
            "alert_age_seconds": 10800,
            "miss_count": 2,
            "hint": "close, re-date, or restore evidence",
            "link": None,
            "already_alerted": True,
        }
    ],
    "slo_breaches": [],
    "failing_evals": [],
    "openrouter_severe": [],
    "stale_prs": [],
    "integrity": [],
    "needs_you": [],
    "trains": [],
    "liveness": {
        "armed_expectations": 0,
        "last_sweep_at": None,
        "last_sweep_age_seconds": None,
    },
}


class StewardFactsComposerTests(TestCase):
    def setUp(self):
        for collector in CollectorStatus.Collector.values:
            CollectorStatus.objects.create(
                collector=collector,
                last_success_at=NOW,
                last_attempt_at=NOW,
            )

    def _missed_heartbeat(self):
        expectation = Expectation.objects.create(
            id=41,
            kind=Expectation.Kind.HEARTBEAT,
            interval_s=1800,
            grace_s=900,
            evidence_source=EvidenceSource.GATEWAY_HEARTBEAT,
            subject="basecamp-pm",
            state=Expectation.State.MISSED,
            last_satisfied_at=NOW - timedelta(days=2, minutes=30),
            miss_count=2,
            last_alerted_at=NOW - timedelta(hours=3),
            on_miss=Expectation.OnMiss.URGENT,
        )
        return expectation

    def test_composer_snapshot_and_renderer_are_one_source_of_truth(self):
        expectation = self._missed_heartbeat()
        AlertState.objects.create(
            fingerprint=f"steward-miss:{expectation.pk}:{expectation.miss_count}",
            last_sent_at=NOW - timedelta(hours=3),
            sent_count=1,
        )

        facts = compose_steward_facts(NOW, SINCE)
        text, stats = render_steward_daily_digest(facts=facts)

        self.assertEqual(facts, FACTS_SNAPSHOT)
        self.assertEqual(stats, FACTS_SNAPSHOT["stats"])
        self.assertEqual(
            text,
            "\n".join(
                [
                    "STEWARD DAILY FACTS",
                    "2026-09-03 UTC",
                    "",
                    "STALLED (1)",
                    "- basecamp-pm — 2d overdue; alerted 3h ago — close, re-date, or restore evidence",
                    "",
                    "Reply on Telegram or run: python manage.py steward_ack <expectation_id> / steward_decide",
                ]
            ),
        )

    def test_already_alerted_is_false_without_confirmed_alert_state(self):
        self._missed_heartbeat()

        facts = compose_steward_facts(NOW, SINCE)
        text, _ = render_steward_daily_digest(facts=facts)

        self.assertFalse(facts["stalled"][0]["already_alerted"])
        self.assertNotIn("; alerted", text)

    def test_recorded_digest_is_the_openrouter_watermark(self):
        DigestRecord.objects.create(
            sent_at=NOW - timedelta(hours=1),
            delivery=DigestRecord.Delivery.RECORDED,
            body="recorded",
            stats={"facts": FACTS_SNAPSHOT},
        )
        payload = {
            "kind": "null_rate",
            "scope": "account",
            "model": "provider/model",
            "current_pct": 3.0,
            "baseline_days": 3,
            "severe": True,
        }
        for name, received_at in (
            ("before-watermark", NOW - timedelta(hours=2)),
            ("after-watermark", NOW - timedelta(minutes=30)),
        ):
            EvidenceEvent.objects.create(
                source=EvidenceSource.OPENROUTER_MODEL_HEALTH,
                subject=f"openrouter:{name}",
                occurred_at=received_at,
                received_at=received_at,
                payload=payload,
                fingerprint=name,
                trust=EvidenceEvent.Trust.AUTHENTICATED_API,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )

        text, stats = render_steward_daily_digest(now=NOW)

        self.assertEqual(stats["openrouter"], 1)
        self.assertIn("null finish_reason 3.00%", text)


@override_settings(STEWARD_INGEST_SECRET="obvious-test-steward-secret")
class StewardFactsEndpointTests(TestCase):
    def setUp(self):
        cache.clear()

    def _headers(self, *, timestamp: int | None = None, signature: str | None = None):
        timestamp_text = str(timestamp if timestamp is not None else int(time.time()))
        if signature is None:
            signature = hmac.new(
                b"obvious-test-steward-secret",
                timestamp_text.encode("ascii") + b".",
                hashlib.sha256,
            ).hexdigest()
        return {
            "X-Steward-Timestamp": timestamp_text,
            "X-Steward-Signature": signature,
        }

    def test_no_snapshot_then_newest_snapshot_with_cache_headers(self):
        no_snapshot = self.client.get("/api/steward/facts/", headers=self._headers())
        self.assertEqual(no_snapshot.status_code, 404)
        self.assertEqual(no_snapshot.json(), {"error": "no_snapshot"})

        DigestRecord.objects.create(
            sent_at=NOW - timedelta(days=1),
            delivery=DigestRecord.Delivery.RECORDED,
            body="older",
            stats={"facts": {**FACTS_SNAPSHOT, "generated_at": "2026-09-02T12:00:00Z"}},
        )
        DigestRecord.objects.create(
            sent_at=NOW,
            delivery=DigestRecord.Delivery.RECORDED,
            body="current",
            stats={"facts": FACTS_SNAPSHOT},
        )
        DigestRecord.objects.create(
            sent_at=NOW + timedelta(hours=1),
            delivery=DigestRecord.Delivery.DELIVERED,
            body="legacy delivery",
            stats={"facts": {**FACTS_SNAPSHOT, "generated_at": "wrong-snapshot"}},
        )

        response = self.client.get("/api/steward/facts/", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), FACTS_SNAPSHOT)
        self.assertRegex(response["ETag"], r'^"[0-9a-f]{32,64}"$')
        self.assertEqual(response["Cache-Control"], "private, max-age=60")

    def test_auth_failures_are_401_and_post_is_405(self):
        missing = self.client.get("/api/steward/facts/")
        bad = self.client.get(
            "/api/steward/facts/",
            headers=self._headers(signature="0" * 64),
        )
        stale = self.client.get(
            "/api/steward/facts/",
            headers=self._headers(timestamp=int(time.time()) - 301),
        )
        with self.settings(STEWARD_INGEST_SECRET=""):
            unconfigured = self.client.get(
                "/api/steward/facts/",
                headers=self._headers(),
            )
        post = self.client.post("/api/steward/facts/", data={}, content_type="application/json")

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(stale.status_code, 401)
        self.assertEqual(unconfigured.status_code, 401)
        self.assertEqual(post.status_code, 405)

    def test_throttle_scope_is_ten_per_minute(self):
        responses = [self.client.get("/api/steward/facts/", headers=self._headers()) for _ in range(11)]

        self.assertTrue(all(response.status_code == 404 for response in responses[:10]))
        self.assertEqual(responses[10].status_code, 429)
