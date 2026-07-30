from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings

from apps.steward.models import EvidenceEvent, EvidenceSource, Expectation
from apps.steward.sweep import run_steward_sweep


@override_settings(STEWARD_DEADMAN_URL="")
class StewardSweepTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

    def _heartbeat(self, **overrides):
        values = {
            "kind": Expectation.Kind.HEARTBEAT,
            "interval_s": 1800,
            "grace_s": 900,
            "evidence_source": EvidenceSource.GATEWAY_HEARTBEAT,
            "subject": "personal-openclaw-gateway",
            "state": Expectation.State.ARMED,
            "last_satisfied_at": self.now,
            "on_miss": Expectation.OnMiss.URGENT,
        }
        values.update(overrides)
        return Expectation.objects.create(**values)

    def _event(
        self,
        *,
        source,
        subject,
        occurred_at,
        provenance=EvidenceEvent.Provenance.COLLECTOR,
        fingerprint="event-1",
    ):
        return EvidenceEvent.objects.create(
            source=source,
            subject=subject,
            occurred_at=occurred_at,
            payload={},
            fingerprint=fingerprint,
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=provenance,
        )

    @patch("apps.steward.sweep.send_urgent", return_value="delivered")
    @patch("apps.steward.sweep.timezone.now")
    def test_heartbeat_miss_cooldown_repeat_and_recovery(self, now_mock, urgent):
        expectation = self._heartbeat(last_satisfied_at=self.now - timedelta(minutes=46))

        now_mock.return_value = self.now
        run_steward_sweep()
        expectation.refresh_from_db()
        self.assertEqual(expectation.state, Expectation.State.MISSED)
        self.assertEqual(expectation.miss_count, 1)
        self.assertEqual(urgent.call_count, 1)

        now_mock.return_value = self.now + timedelta(hours=1)
        run_steward_sweep()
        self.assertEqual(urgent.call_count, 1)

        now_mock.return_value = self.now + timedelta(hours=6, minutes=1)
        run_steward_sweep()
        self.assertEqual(urgent.call_count, 2)

        recovered_at = self.now + timedelta(hours=6, minutes=2)
        self._event(
            source=EvidenceSource.GATEWAY_HEARTBEAT,
            subject=expectation.subject,
            occurred_at=recovered_at,
            fingerprint="heartbeat-recovered",
        )
        now_mock.return_value = recovered_at
        run_steward_sweep()
        expectation.refresh_from_db()
        self.assertEqual(expectation.state, Expectation.State.ARMED)
        self.assertEqual(expectation.last_satisfied_at, recovered_at)
        self.assertIsNone(expectation.last_alerted_at)
        self.assertEqual(urgent.call_count, 3)
        self.assertTrue(urgent.call_args.args[0].startswith("Steward recovery:"))

    @patch("apps.steward.sweep.send_urgent")
    @patch("apps.steward.sweep.timezone.now")
    def test_deadline_miss_is_recorded_without_digest_message(
        self,
        now_mock,
        urgent,
    ):
        deadline = Expectation.objects.create(
            kind=Expectation.Kind.DEADLINE,
            due_at=self.now - timedelta(days=2),
            grace_s=86400,
            evidence_source=EvidenceSource.ASC_VERSION_STATE,
            subject="nbhd-ios-2.1.5-rollout",
            on_miss=Expectation.OnMiss.DIGEST,
        )
        now_mock.return_value = self.now

        summary = run_steward_sweep()

        deadline.refresh_from_db()
        self.assertEqual(deadline.state, Expectation.State.MISSED)
        self.assertEqual(deadline.miss_count, 1)
        self.assertEqual(summary["digest_misses"], 1)
        urgent.assert_not_called()

    @patch("apps.steward.sweep.send_urgent")
    @patch("apps.steward.sweep.timezone.now")
    def test_recurrence_uses_latest_window_and_digest_never_sends(
        self,
        now_mock,
        urgent,
    ):
        recurrence = Expectation.objects.create(
            kind=Expectation.Kind.RECURRENCE,
            interval_s=7 * 86400,
            grace_s=86400,
            evidence_source=EvidenceSource.CI_RUN,
            subject="nbhd-united-main-ci",
            last_satisfied_at=self.now - timedelta(days=9),
            on_miss=Expectation.OnMiss.DIGEST,
        )
        now_mock.return_value = self.now
        run_steward_sweep()
        recurrence.refresh_from_db()
        self.assertEqual(recurrence.state, Expectation.State.MISSED)
        urgent.assert_not_called()

        current_evidence = self.now + timedelta(minutes=1)
        self._event(
            source=EvidenceSource.CI_RUN,
            subject=recurrence.subject,
            occurred_at=current_evidence,
            fingerprint="green-main-ci",
        )
        now_mock.return_value = current_evidence
        run_steward_sweep()
        recurrence.refresh_from_db()
        self.assertEqual(recurrence.state, Expectation.State.ARMED)
        self.assertEqual(recurrence.last_satisfied_at, current_evidence)
        urgent.assert_not_called()

    @patch("apps.steward.sweep.send_urgent")
    @patch("apps.steward.sweep.timezone.now")
    def test_agent_proposed_event_cannot_satisfy(self, now_mock, urgent):
        expectation = self._heartbeat(
            state=Expectation.State.MISSED,
            last_satisfied_at=self.now - timedelta(hours=1),
            miss_count=1,
            last_alerted_at=self.now,
        )
        self._event(
            source=EvidenceSource.GATEWAY_HEARTBEAT,
            subject=expectation.subject,
            occurred_at=self.now + timedelta(minutes=1),
            provenance=EvidenceEvent.Provenance.AGENT_PROPOSED,
            fingerprint="agent-claim",
        )
        now_mock.return_value = self.now + timedelta(minutes=1)

        run_steward_sweep()

        expectation.refresh_from_db()
        self.assertEqual(expectation.state, Expectation.State.MISSED)
        self.assertNotEqual(
            expectation.last_satisfied_at,
            self.now + timedelta(minutes=1),
        )
        urgent.assert_not_called()

    @override_settings(STEWARD_DEADMAN_URL="https://deadman.example/ping")
    @patch("apps.steward.sweep.requests.get")
    def test_successful_sweep_pings_deadman_and_swallow_failures(self, get):
        get.side_effect = requests.Timeout("down")

        run_steward_sweep()

        get.assert_called_once_with("https://deadman.example/ping", timeout=5)
