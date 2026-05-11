"""Regression coverage for OpenClaw 5.7 missed-cron catch-up on image swap.

The bug (canary 2026-05-11 evening check-in incident):
``apply_single_tenant_image_task`` snapshots the cron registry pre-swap and
``restore_crons_after_image_update_task`` re-adds them post-swap. The snapshot
strips ``state`` (including ``lastRunAtMs``) via ``_STRIP_FIELDS``. OpenClaw
5.7's startup catch-up (``planStartupCatchup``) gates the missed-fire path on
``allowCronMissedRunByLastRun`` which requires ``lastRunAtMs`` to be set —
without it, ``isRunnableJob`` returns false and the missed fire is silently
dropped. So a cron that should have fired during the image-swap window simply
doesn't, even though it's correctly registered in the new container.

The fix detects missed fires via ``croniter`` between ``snapshot_at`` and now,
then calls ``cron.run`` for each (capped) so the agent turn actually executes.

These tests pin the catch-up contract end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.cron.gateway_client import GatewayError
from apps.orchestrator.tasks import (
    _MAX_MISSED_FIRES_PER_RESTORE,
    _compute_missed_cron_fires,
    restore_crons_after_image_update_task,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _list_response(jobs: list[dict]) -> dict:
    """Wrap a job list in OpenClaw's cron.list response envelope."""
    return {"jobs": jobs, "total": len(jobs)}


def _job(
    name: str,
    *,
    schedule_expr: str = "0 7 * * *",
    tz: str = "UTC",
    kind: str = "cron",
    enabled: bool = True,
    gateway_id: str | None = None,
) -> dict:
    """Build a snapshot job dict (post-snapshot, pre-restore shape)."""
    schedule: dict = {"kind": kind, "tz": tz}
    if kind == "cron":
        schedule["expr"] = schedule_expr
    return {
        "name": name,
        "schedule": schedule,
        "sessionTarget": "isolated",
        "payload": {"kind": "agentTurn", "message": "test"},
        "delivery": {"mode": "none"},
        "enabled": enabled,
        "id": gateway_id or f"old-{name.replace(' ', '-').lower()}",
    }


def _gateway_job(name: str, *, gateway_id: str) -> dict:
    """Job as returned by cron.list AFTER restore (fresh UUIDs)."""
    return {**_job(name, gateway_id=gateway_id), "id": gateway_id}


class ComputeMissedCronFiresTests(TestCase):
    """Pure helper tests — no Django or gateway dependency."""

    def test_returns_latest_missed_fire_per_name(self):
        """An hourly cron with a 2.5h gap should yield exactly 2 missed fires,
        and we keep only the latest (users want the most recent reminder)."""
        snapshot_at = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
        now = datetime(2026, 5, 11, 12, 30, 0, tzinfo=UTC)
        jobs = [_job("Hourly Thing", schedule_expr="0 * * * *", tz="UTC")]

        missed = _compute_missed_cron_fires(jobs, snapshot_at, now)

        # Fires at 11:00 and 12:00 are in (10:00, 12:30]; latest is 12:00.
        self.assertIn("Hourly Thing", missed)
        self.assertEqual(missed["Hourly Thing"].hour, 12)

    def test_skips_when_next_fire_is_in_future(self):
        """No missed fire when the schedule's next occurrence is past `now`."""
        snapshot_at = datetime(2026, 5, 11, 8, 0, 0, tzinfo=UTC)
        now = datetime(2026, 5, 11, 9, 0, 0, tzinfo=UTC)
        # 0 12 * * * (noon UTC) — between 08:00 and 09:00 no fire occurred
        jobs = [_job("Lunch Reminder", schedule_expr="0 12 * * *")]

        missed = _compute_missed_cron_fires(jobs, snapshot_at, now)

        self.assertEqual(missed, {})

    def test_skips_disabled_crons(self):
        snapshot_at = datetime(2026, 5, 11, 6, 0, 0, tzinfo=UTC)
        now = datetime(2026, 5, 11, 8, 0, 0, tzinfo=UTC)
        jobs = [_job("Off", schedule_expr="0 7 * * *", enabled=False)]

        self.assertEqual(_compute_missed_cron_fires(jobs, snapshot_at, now), {})

    def test_skips_non_cron_kinds(self):
        """`at` and `every` aren't fire-time-aligned in the same sense.

        `at` is handled by PR #513's wake-sweep; `every` lacks the
        absolute-time anchor that makes "missed fire" meaningful.
        """
        snapshot_at = datetime(2026, 5, 11, 6, 0, 0, tzinfo=UTC)
        now = datetime(2026, 5, 11, 8, 0, 0, tzinfo=UTC)
        jobs = [
            _job("One Shot", kind="at"),
            _job("Every Hour", kind="every"),
        ]

        self.assertEqual(_compute_missed_cron_fires(jobs, snapshot_at, now), {})

    def test_skips_sync_and_fuel_prefixed(self):
        """Agent-managed one-shots (_sync:*, _fuel:*) must not be refired —
        their originating session is over by the time we'd catch up."""
        snapshot_at = datetime(2026, 5, 11, 6, 0, 0, tzinfo=UTC)
        now = datetime(2026, 5, 11, 8, 0, 0, tzinfo=UTC)
        jobs = [
            _job("_sync:Morning Briefing", schedule_expr="0 7 * * *"),
            _job("_fuel:welcome", schedule_expr="0 7 * * *"),
            _job("Morning Briefing", schedule_expr="0 7 * * *"),
        ]

        missed = _compute_missed_cron_fires(jobs, snapshot_at, now)

        self.assertEqual(set(missed.keys()), {"Morning Briefing"})

    def test_handles_timezone_local_schedule(self):
        """A `0 7 * * * Asia/Tokyo` cron's fire time in the snapshot window
        is correctly converted regardless of snapshot/now being UTC."""
        # 07:00 JST on 2026-05-11 = 22:00 UTC on 2026-05-10
        snapshot_at = datetime(2026, 5, 10, 21, 56, 0, tzinfo=UTC)
        now = datetime(2026, 5, 10, 22, 30, 0, tzinfo=UTC)
        jobs = [_job("Morning Briefing", schedule_expr="0 7 * * *", tz="Asia/Tokyo")]

        missed = _compute_missed_cron_fires(jobs, snapshot_at, now)

        self.assertIn("Morning Briefing", missed)


class RestoreFiresMissedCronsTests(TestCase):
    """End-to-end: restore_crons_after_image_update_task triggers cron.run for missed fires."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Catchup Test", telegram_chat_id=864213579)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-catchup-test"
        self.tenant.container_fqdn = "oc-catchup-test.internal"
        # Snapshot taken 2.5h ago; the cron in the snapshot fires at 7:00 JST
        # (= 22:00 UTC) so a snapshot at 21:56 UTC with now at ~22:30 UTC
        # makes the 22:00 UTC fire fall inside the window.
        self.snapshot_at = timezone.now() - timedelta(hours=2, minutes=30)
        self.tenant.cron_jobs_snapshot = {
            "snapshot_at": self.snapshot_at.isoformat(),
            "trigger": "pre-image-update",
            "image_tag": "test-tag",
            "jobs": [
                _job(
                    "Hourly Thing",
                    schedule_expr="0 * * * *",
                    tz="UTC",
                    gateway_id="old-uuid-hourly",
                ),
            ],
        }
        self.tenant.save()

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_fires_missed_cron_after_restore(self, mock_invoke):
        """The full path: pre-restore cron.list → cron.add → dedup → catch-up cron.list → cron.run."""
        # Sequence of responses: pre-restore cron.list (empty), cron.add ok,
        # dedup's cron.list (now has the restored job), catch-up's cron.list
        # (same), cron.run ok.
        responses = [
            _list_response([]),  # pre-restore: container is empty
            {"id": "new-uuid-hourly"},  # cron.add
            _list_response([_gateway_job("Hourly Thing", gateway_id="new-uuid-hourly")]),  # dedup cron.list
            _list_response([_gateway_job("Hourly Thing", gateway_id="new-uuid-hourly")]),  # catch-up cron.list
            {"ok": True},  # cron.run
        ]
        mock_invoke.side_effect = responses

        restore_crons_after_image_update_task(str(self.tenant.id))

        # Confirm cron.run was called with the NEW gateway id (not old-uuid-hourly)
        run_calls = [c for c in mock_invoke.call_args_list if c.args[1] == "cron.run"]
        self.assertEqual(len(run_calls), 1, f"Expected exactly 1 cron.run; got calls: {mock_invoke.call_args_list}")
        self.assertEqual(run_calls[0].args[2], {"jobId": "new-uuid-hourly"})

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_no_fire_when_snapshot_at_in_future(self, mock_invoke):
        """Clock skew defense: snapshot timestamp ahead of now → bail out."""
        self.tenant.cron_jobs_snapshot["snapshot_at"] = (timezone.now() + timedelta(hours=1)).isoformat()
        self.tenant.save()

        mock_invoke.side_effect = [
            _list_response([]),  # pre-restore cron.list
            {"id": "new"},  # cron.add
            _list_response([_gateway_job("Hourly Thing", gateway_id="new")]),  # dedup
        ]

        restore_crons_after_image_update_task(str(self.tenant.id))

        run_calls = [c for c in mock_invoke.call_args_list if c.args[1] == "cron.run"]
        self.assertEqual(run_calls, [])

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_caps_at_max_missed_fires(self, mock_invoke):
        """A 10h gap on an hourly cron + 5 other crons would yield many
        missed fires; we cap total triggers to avoid a stampede."""
        # Generate _MAX + 3 distinct hourly crons, all with a fire in window.
        jobs = [
            _job(
                f"Cron {i}",
                schedule_expr="0 * * * *",
                gateway_id=f"old-{i}",
            )
            for i in range(_MAX_MISSED_FIRES_PER_RESTORE + 3)
        ]
        self.tenant.cron_jobs_snapshot["jobs"] = jobs
        self.tenant.save()

        fresh = [_gateway_job(f"Cron {i}", gateway_id=f"new-{i}") for i in range(len(jobs))]
        responses = [
            _list_response([]),  # pre-restore cron.list
            *[{"id": f"new-{i}"} for i in range(len(jobs))],  # cron.adds
            _list_response(fresh),  # dedup cron.list
            _list_response(fresh),  # catch-up cron.list
            *[{"ok": True} for _ in range(_MAX_MISSED_FIRES_PER_RESTORE)],  # cron.runs (capped)
        ]
        mock_invoke.side_effect = responses

        restore_crons_after_image_update_task(str(self.tenant.id))

        run_calls = [c for c in mock_invoke.call_args_list if c.args[1] == "cron.run"]
        self.assertEqual(len(run_calls), _MAX_MISSED_FIRES_PER_RESTORE)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_continues_when_individual_cron_run_fails(self, mock_invoke):
        """One failing cron.run shouldn't kill the rest of the catch-up batch."""
        jobs = [
            _job("Cron A", schedule_expr="0 * * * *", gateway_id="old-a"),
            _job("Cron B", schedule_expr="0 * * * *", gateway_id="old-b"),
        ]
        self.tenant.cron_jobs_snapshot["jobs"] = jobs
        self.tenant.save()

        fresh = [
            _gateway_job("Cron A", gateway_id="new-a"),
            _gateway_job("Cron B", gateway_id="new-b"),
        ]
        responses = [
            _list_response([]),  # pre-restore cron.list
            {"id": "new-a"},
            {"id": "new-b"},  # two cron.adds
            _list_response(fresh),  # dedup cron.list
            _list_response(fresh),  # catch-up cron.list
            GatewayError("503 first run failed"),  # cron.run #1 fails
            {"ok": True},  # cron.run #2 succeeds
        ]
        mock_invoke.side_effect = responses

        # Should NOT raise — the failure is logged and the loop continues.
        restore_crons_after_image_update_task(str(self.tenant.id))

        run_calls = [c for c in mock_invoke.call_args_list if c.args[1] == "cron.run"]
        self.assertEqual(len(run_calls), 2)
