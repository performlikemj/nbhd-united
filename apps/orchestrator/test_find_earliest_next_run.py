"""Tests for ``_find_earliest_next_run`` against the real OpenClaw cron.list shape.

Regression guard for the wake-time bug where the cron-aware re-hibernation
path was reading ``job["nextRunAtMs"]`` from the wrong field. OpenClaw's
gateway returns each job with its mutable runtime state nested under
``state``:

    {"id": ..., "name": ..., "schedule": {...}, "enabled": true,
     "state": {"nextRunAtMs": 1746690000000, "lastRunAtMs": ..., ...}}

The previous code read the top-level ``nextRunAtMs``, always got ``None``,
and fell back to the croniter-from-``schedule.expr`` path. That fallback
silently masked the bug for ``cron`` schedules (croniter can recompute) but
broke ``every`` and ``at`` schedules (no expression to fall back on), so
those tenants got no cron-aware wake scheduled at all and stayed
hibernated until the next user message.

Fixtures here mirror the real shape the gateway sends. The existing
hibernation tests in ``test_hibernation.py`` use a flat-shape fixture
(no ``state``) and never exercised this path directly, which is how the
bug shipped.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.hibernation import _find_earliest_next_run


def _job(
    name: str,
    *,
    next_run_ms: int | None,
    schedule: dict | None = None,
    enabled: bool = True,
) -> dict:
    """Build a job dict in the same shape the gateway returns.

    ``next_run_ms`` lands under ``state.nextRunAtMs`` — that's the only
    place the runtime exposes it.
    """
    job: dict = {
        "id": f"job-{name}",
        "name": name,
        "enabled": enabled,
        "schedule": schedule or {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
        "state": {},
    }
    if next_run_ms is not None:
        job["state"]["nextRunAtMs"] = next_run_ms
    return job


class FindEarliestNextRunTests(TestCase):
    """Direct tests of ``_find_earliest_next_run``."""

    def setUp(self):
        self.now_ms = 1_746_700_000_000  # arbitrary fixed "now"

    def test_reads_next_run_from_state_nested_field(self):
        """The whole point — picks up state.nextRunAtMs, not top-level."""
        future_ms = self.now_ms + 3600_000  # 1h from now
        jobs = [_job("Morning Briefing", next_run_ms=future_ms)]
        self.assertEqual(_find_earliest_next_run(jobs, self.now_ms), future_ms)

    def test_picks_earliest_across_jobs(self):
        soon_ms = self.now_ms + 600_000  # 10 min
        later_ms = self.now_ms + 3600_000  # 1 h
        jobs = [
            _job("Later", next_run_ms=later_ms),
            _job("Soon", next_run_ms=soon_ms),
        ]
        self.assertEqual(_find_earliest_next_run(jobs, self.now_ms), soon_ms)

    def test_skips_disabled_jobs(self):
        future_ms = self.now_ms + 600_000
        jobs = [_job("Disabled", next_run_ms=future_ms, enabled=False)]
        self.assertIsNone(_find_earliest_next_run(jobs, self.now_ms))

    def test_skips_past_next_run(self):
        """``nextRunAtMs`` may have already fired — don't schedule a wake to the past.

        Uses an ``every`` schedule (no ``expr``) so the croniter fallback
        can't paper over a stale state by recomputing.
        """
        past_ms = self.now_ms - 60_000
        jobs = [
            _job(
                "Past",
                schedule={"kind": "every", "intervalMs": 900_000},
                next_run_ms=past_ms,
            )
        ]
        self.assertIsNone(_find_earliest_next_run(jobs, self.now_ms))

    def test_every_kind_with_state_next_run_works(self):
        """``every`` schedules have no ``expr`` so croniter fallback can't compute one
        — the only way the wake gets scheduled is via state.nextRunAtMs.

        This was the silently-broken case before the fix: every-kind crons
        produced no wake, leaving hibernated tenants stuck."""
        future_ms = self.now_ms + 600_000
        jobs = [
            _job(
                "Heartbeat",
                schedule={"kind": "every", "intervalMs": 900_000},
                next_run_ms=future_ms,
            )
        ]
        self.assertEqual(_find_earliest_next_run(jobs, self.now_ms), future_ms)

    def test_at_kind_with_state_next_run_works(self):
        """``at`` (one-shot) schedules also have no ``expr``."""
        future_ms = self.now_ms + 600_000
        jobs = [
            _job(
                "_finance:welcome",
                schedule={"kind": "at", "atMs": future_ms},
                next_run_ms=future_ms,
            )
        ]
        self.assertEqual(_find_earliest_next_run(jobs, self.now_ms), future_ms)

    def test_falls_back_to_croniter_when_state_next_run_missing(self):
        """Snapshot-fed jobs (after a stale persist) may not have state.nextRunAtMs.
        For cron-kind schedules, recompute from the expression so the wake still arms.
        """
        jobs = [
            {
                "name": "Morning Briefing",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                # No state at all — simulates a snapshot-only job.
            }
        ]
        with patch(
            "apps.orchestrator.hibernation._next_run_from_expr",
            return_value=self.now_ms + 1_800_000,
        ) as mock_compute:
            result = _find_earliest_next_run(jobs, self.now_ms)
        self.assertEqual(result, self.now_ms + 1_800_000)
        mock_compute.assert_called_once()

    def test_top_level_next_run_is_ignored(self):
        """Defensive check: the wrong field name (top-level ``nextRunAtMs``)
        must not be picked up. If we ever go back to reading the top level,
        this test fails — that's the bug we just fixed."""
        future_ms = self.now_ms + 600_000
        jobs = [
            {
                "name": "Trap",
                "enabled": True,
                # Schedule with no expr so the croniter fallback can't paper over.
                "schedule": {"kind": "every", "intervalMs": 900_000},
                "nextRunAtMs": future_ms,  # WRONG location — must be ignored
            }
        ]
        self.assertIsNone(_find_earliest_next_run(jobs, self.now_ms))

    def test_empty_input_returns_none(self):
        self.assertIsNone(_find_earliest_next_run([], self.now_ms))

    def test_mixed_kinds_picks_correct_minimum(self):
        """All three schedule kinds in one fleet, varying state.nextRunAtMs."""
        in_30min = self.now_ms + 1_800_000
        in_2h = self.now_ms + 7_200_000
        in_5min = self.now_ms + 300_000
        jobs = [
            _job(
                "Morning Briefing",
                schedule={"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                next_run_ms=in_30min,
            ),
            _job(
                "Heartbeat",
                schedule={"kind": "every", "intervalMs": 900_000},
                next_run_ms=in_5min,  # earliest
            ),
            _job(
                "_fuel:welcome",
                schedule={"kind": "at", "atMs": in_2h},
                next_run_ms=in_2h,
            ),
        ]
        self.assertEqual(_find_earliest_next_run(jobs, self.now_ms), in_5min)
