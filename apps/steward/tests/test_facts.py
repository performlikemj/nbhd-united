from __future__ import annotations

import hashlib
import hmac
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import path

from apps.friends.models import ContentReport
from apps.steward.digest import render_steward_daily_digest, run_steward_daily_digest
from apps.steward.facts import compose_steward_facts
from apps.steward.models import (
    AlertState,
    CollectorStatus,
    DigestRecord,
    EvidenceEvent,
    EvidenceSource,
    Expectation,
)
from apps.tenants.models import Tenant, User

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
        "content_reports": 0,
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
    "content_reports": [],
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

    def _report(self, *, status="open", target_kind="general", age=timedelta(hours=25), resolved_at=None):
        index = ContentReport.objects.count()
        user = User.objects.create_user(
            username=f"private-reporter-{index}",
            email=f"private-reporter-{index}@example.com",
            display_name="Private Reporter",
        )
        tenant = Tenant.objects.create(user=user, status="active")
        report = ContentReport.objects.create(
            reporter_tenant=tenant,
            reporter_user=user,
            target_kind=target_kind,
            reason="Private report text and message content must never appear",
            status=status,
            resolved_at=resolved_at,
        )
        ContentReport.objects.filter(pk=report.pk).update(created_at=NOW - age)
        return report

    def test_content_reports_include_open_and_reporter_hidden_but_not_resolved_or_dismissed(self):
        oldest = self._report(age=timedelta(days=30))
        hidden = self._report(status="hidden", target_kind="shared_lesson")
        message = self._report(status="hidden", target_kind="friend_message")
        self._report(status="dismissed")
        self._report(status="resolved")
        self._report(resolved_at=NOW)
        self._report(status="hidden", resolved_at=NOW)

        facts = compose_steward_facts(NOW, SINCE)

        self.assertEqual(facts["version"], 1)
        self.assertEqual(facts["stats"]["content_reports"], 3)
        self.assertEqual(facts["content_reports"][0]["id"], f"content-report:{oldest.pk}")
        self.assertEqual(
            {item["id"] for item in facts["content_reports"]},
            {f"content-report:{report.pk}" for report in (oldest, hidden, message)},
        )
        self.assertEqual(facts["content_reports"][0]["age_seconds"], 30 * 86400)

    def test_content_report_values_are_metadata_only(self):
        report = self._report()

        item = compose_steward_facts(NOW, SINCE)["content_reports"][0]

        # Exact allowlist: IDs, ISO timestamps, numbers, enums, and fixed copy/nulls.
        # This also rejects any newly added reporter, reason, or content fields.
        self.assertEqual(
            item,
            {
                "id": f"content-report:{report.pk}",
                "created_at": "2026-09-02T11:00:00Z",
                "age_seconds": 90000,
                "category": None,
                "target_kind": "general",
                "status": "open",
                "hint": "read the report, then hide the content, block/warn the user, or dismiss (24h promise)",
                "link": None,
                "already_alerted": False,
            },
        )
        self.assertIs(type(item["age_seconds"]), int)
        self.assertIs(item["already_alerted"], False)
        self.assertEqual(datetime.fromisoformat(item["created_at"]), NOW - timedelta(hours=25))

    def test_content_report_unknown_target_does_not_leak_free_text(self):
        self._report(target_kind="private text")

        item = compose_steward_facts(NOW, SINCE)["content_reports"][0]

        self.assertEqual(item["target_kind"], "unknown")

    def test_content_report_links_to_admin_when_registered(self):
        report = self._report()
        site = AdminSite()
        site.register(ContentReport)
        urls = type("ReportAdminURLs", (), {"urlpatterns": [path("admin/", site.urls)]})

        with self.settings(ROOT_URLCONF=urls), patch("apps.steward.facts.admin.site", site):
            item = compose_steward_facts(NOW, SINCE)["content_reports"][0]

        self.assertEqual(item["link"], f"/admin/friends/contentreport/{report.pk}/change/")

    def test_digest_reports_follow_stalled_and_use_category_fallback(self):
        self._missed_heartbeat()
        report = self._report()
        facts = compose_steward_facts(NOW, SINCE)

        text, stats = render_steward_daily_digest(facts=facts)

        self.assertEqual(stats["content_reports"], 1)
        self.assertIn(
            f"\n\nREPORTS (1)\n- content-report:{report.pk} — report on general — 1d old "
            "— read the report, then hide, block/warn, or dismiss\n\n",
            text,
        )
        self.assertLess(text.index("STALLED (1)"), text.index("REPORTS (1)"))
        facts["content_reports"][0]["category"] = "spam"
        text, _ = render_steward_daily_digest(facts=facts)
        self.assertIn("— spam on general —", text)

    def test_empty_reports_preserve_legacy_digest_bytes(self):
        legacy = deepcopy(FACTS_SNAPSHOT)
        del legacy["content_reports"]
        del legacy["stats"]["content_reports"]

        text, _ = render_steward_daily_digest(facts=FACTS_SNAPSHOT)
        legacy_text, _ = render_steward_daily_digest(facts=legacy)

        self.assertEqual(text.encode(), legacy_text.encode())
        self.assertNotIn("REPORTS", text)

    @patch("apps.steward.digest.collect_eval_evidence", return_value={"created": 0})
    def test_daily_runner_records_content_reports(self, _collect):
        report = self._report()

        with patch("apps.steward.digest.timezone.now", return_value=NOW):
            result = run_steward_daily_digest()

        record = DigestRecord.objects.get(pk=result["digest_id"])
        self.assertEqual(record.delivery, DigestRecord.Delivery.RECORDED)
        self.assertEqual(record.stats["facts"]["content_reports"][0]["id"], f"content-report:{report.pk}")
        self.assertIn("REPORTS (1)", record.body)
        self.assertEqual(record.body, render_steward_daily_digest(facts=record.stats["facts"])[0])

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
