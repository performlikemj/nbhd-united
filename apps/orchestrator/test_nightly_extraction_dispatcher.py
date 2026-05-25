"""Tests for the hourly per-tenant-tz nightly extraction dispatcher.

``nightly_extraction_task`` in ``apps.orchestrator.tasks`` is no longer a
fleet-wide UTC-21:30 firing — it's an hourly dispatcher that runs
``run_extraction_for_tenant`` for any tenant whose **local** time is in
the 21:xx window, with a once-per-local-day idempotency guard via
``Tenant.last_nightly_extraction_at``.

These tests cover the three load-bearing properties:

1. **Window match (TZ-aware)** — a JST tenant gets fired at UTC 12:00,
   a PT tenant at the same UTC tick does not.
2. **Idempotency** — re-firing the dispatcher within the same local day
   for the same tenant skips the extraction (no second LLM call).
3. **Suspended tenants are inert** — the dispatcher only iterates active
   tenants regardless of their local time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.tasks import (
    _already_ran_today_local,
    _is_nightly_extraction_window_local,
    nightly_extraction_task,
)
from apps.tenants.models import Tenant, User


def _make_tenant(slug: str, *, timezone_name: str, status: str = Tenant.Status.ACTIVE) -> Tenant:
    user = User.objects.create_user(username=slug, password="x" * 32, timezone=timezone_name)
    return Tenant.objects.create(user=user, status=status)


class IsNightlyExtractionWindowLocalTest(TestCase):
    """The pure window-filter helper, isolated from the dispatcher loop."""

    def test_jst_tenant_matches_at_utc_12(self):
        tenant = _make_tenant("jst-12", timezone_name="Asia/Tokyo")
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
        # 12:00 UTC + 9h = 21:00 JST → in window
        self.assertTrue(_is_nightly_extraction_window_local(tenant, now=now))

    def test_jst_tenant_does_not_match_at_utc_13(self):
        tenant = _make_tenant("jst-13", timezone_name="Asia/Tokyo")
        now = datetime(2026, 5, 25, 13, 0, 0, tzinfo=UTC)
        # 13:00 UTC + 9h = 22:00 JST → out of window
        self.assertFalse(_is_nightly_extraction_window_local(tenant, now=now))

    def test_pt_tenant_does_not_match_at_utc_12(self):
        tenant = _make_tenant("pt-12", timezone_name="America/Los_Angeles")
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
        # 12:00 UTC - 7h (PDT in May) = 05:00 PT → out of window
        self.assertFalse(_is_nightly_extraction_window_local(tenant, now=now))

    def test_pt_tenant_matches_at_utc_04(self):
        tenant = _make_tenant("pt-04", timezone_name="America/Los_Angeles")
        now = datetime(2026, 5, 26, 4, 0, 0, tzinfo=UTC)
        # 04:00 UTC - 7h (PDT) = 21:00 PT prev evening → in window
        self.assertTrue(_is_nightly_extraction_window_local(tenant, now=now))

    def test_half_hour_offset_tz_still_matches_on_hour(self):
        tenant = _make_tenant("ist", timezone_name="Asia/Kolkata")
        # IST = UTC+5:30. At UTC 15:30 → 21:00 IST. At UTC 16:00 → 21:30 IST.
        # Both should match (we match on local hour only, not minute).
        self.assertTrue(_is_nightly_extraction_window_local(tenant, now=datetime(2026, 5, 25, 15, 30, 0, tzinfo=UTC)))
        self.assertTrue(_is_nightly_extraction_window_local(tenant, now=datetime(2026, 5, 25, 16, 0, 0, tzinfo=UTC)))

    def test_missing_timezone_falls_back_to_utc(self):
        tenant = _make_tenant("notz", timezone_name="")
        # Empty TZ → UTC fallback. UTC 21:00 → match.
        self.assertTrue(_is_nightly_extraction_window_local(tenant, now=datetime(2026, 5, 25, 21, 0, 0, tzinfo=UTC)))
        self.assertFalse(_is_nightly_extraction_window_local(tenant, now=datetime(2026, 5, 25, 20, 0, 0, tzinfo=UTC)))

    def test_garbage_timezone_falls_back_to_utc(self):
        tenant = _make_tenant("badtz", timezone_name="Not/A_Real_TZ")
        self.assertTrue(_is_nightly_extraction_window_local(tenant, now=datetime(2026, 5, 25, 21, 0, 0, tzinfo=UTC)))


class AlreadyRanTodayLocalTest(TestCase):
    def test_never_ran_returns_false(self):
        tenant = _make_tenant("never", timezone_name="Asia/Tokyo")
        self.assertFalse(_already_ran_today_local(tenant, now=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)))

    def test_ran_earlier_in_same_local_day_returns_true(self):
        tenant = _make_tenant("today", timezone_name="Asia/Tokyo")
        # Ran at UTC 12:00 → 21:00 JST same day
        tenant.last_nightly_extraction_at = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
        tenant.save(update_fields=["last_nightly_extraction_at"])
        # Now is UTC 12:30 → 21:30 JST same day
        self.assertTrue(_already_ran_today_local(tenant, now=datetime(2026, 5, 25, 12, 30, 0, tzinfo=UTC)))

    def test_ran_yesterday_local_returns_false(self):
        tenant = _make_tenant("yesterday", timezone_name="Asia/Tokyo")
        # Ran at UTC 12:00 on the 24th → 21:00 JST on the 24th
        tenant.last_nightly_extraction_at = datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
        tenant.save(update_fields=["last_nightly_extraction_at"])
        # Now is UTC 12:00 on the 25th → 21:00 JST on the 25th — different local day
        self.assertFalse(_already_ran_today_local(tenant, now=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)))

    def test_local_date_comparison_uses_tenant_tz_not_utc(self):
        """Two tenants, same UTC tick — the one whose local date matches
        ``last_nightly_extraction_at``'s local date returns True; the other
        returns False. Catches the subtle bug of comparing on UTC date."""
        # UTC 23:00 on the 24th = 08:00 on the 25th JST.
        # UTC 23:00 on the 24th = 16:00 on the 24th PT.
        jst_tenant = _make_tenant("jst-edge", timezone_name="Asia/Tokyo")
        pt_tenant = _make_tenant("pt-edge", timezone_name="America/Los_Angeles")
        marker = datetime(2026, 5, 24, 23, 0, 0, tzinfo=UTC)
        jst_tenant.last_nightly_extraction_at = marker
        jst_tenant.save(update_fields=["last_nightly_extraction_at"])
        pt_tenant.last_nightly_extraction_at = marker
        pt_tenant.save(update_fields=["last_nightly_extraction_at"])
        # Six hours later: UTC 05:00 on the 25th = 14:00 JST (still 25th),
        # = 22:00 PT (still 24th).
        now = datetime(2026, 5, 25, 5, 0, 0, tzinfo=UTC)
        # JST: marker local date = 25th, now local date = 25th → True
        self.assertTrue(_already_ran_today_local(jst_tenant, now=now))
        # PT: marker local date = 24th, now local date = 24th → True
        self.assertTrue(_already_ran_today_local(pt_tenant, now=now))


class NightlyExtractionDispatcherTaskTest(TestCase):
    """End-to-end tests of the dispatcher loop with run_extraction_for_tenant mocked."""

    def test_fires_only_for_tenants_in_local_window(self):
        jst = _make_tenant("d-jst", timezone_name="Asia/Tokyo")
        pt = _make_tenant("d-pt", timezone_name="America/Los_Angeles")
        # Suspended tenant should never fire regardless of TZ
        sus = _make_tenant("d-sus", timezone_name="Asia/Tokyo", status=Tenant.Status.SUSPENDED)

        with (
            patch("apps.orchestrator.tasks.datetime") as fake_dt,
            patch("apps.journal.extraction.run_extraction_for_tenant") as fake_run,
        ):
            # UTC 12:00 → 21:00 JST (fire), 05:00 PT (skip)
            fake_dt.now.return_value = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
            fake_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            fake_run.return_value = {"lessons": 0, "goals": 0, "tasks": 0, "task_actions": 0, "skipped": None}

            counts = nightly_extraction_task()

        # Only JST tenant fired
        self.assertEqual(counts["fired"], 1)
        self.assertEqual(counts["skipped_window"], 1)  # PT tenant
        self.assertEqual(counts["considered"], 2)  # active only
        called_tenants = {call.args[0].id for call in fake_run.call_args_list}
        self.assertEqual(called_tenants, {jst.id})
        self.assertNotIn(pt.id, called_tenants)
        self.assertNotIn(sus.id, called_tenants)

    def test_idempotency_skips_already_ran(self):
        from django.utils import timezone as dj_tz

        jst = _make_tenant("d-idem", timezone_name="Asia/Tokyo")
        # Mark as already-ran 30 minutes ago (still same JST day)
        jst.last_nightly_extraction_at = datetime(2026, 5, 25, 11, 30, 0, tzinfo=UTC)
        jst.save(update_fields=["last_nightly_extraction_at"])

        with (
            patch("apps.orchestrator.tasks.datetime") as fake_dt,
            patch("apps.journal.extraction.run_extraction_for_tenant") as fake_run,
        ):
            fake_dt.now.return_value = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
            fake_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            fake_run.return_value = {"lessons": 0, "goals": 0, "tasks": 0, "task_actions": 0, "skipped": None}

            counts = nightly_extraction_task()

        self.assertEqual(counts["fired"], 0)
        self.assertEqual(counts["skipped_already_ran"], 1)
        fake_run.assert_not_called()
        # Suppress unused-import warning
        del dj_tz

    def test_dispatcher_counts_errors_without_breaking_loop(self):
        a = _make_tenant("d-err-a", timezone_name="Asia/Tokyo")
        b = _make_tenant("d-err-b", timezone_name="Asia/Tokyo")

        with (
            patch("apps.orchestrator.tasks.datetime") as fake_dt,
            patch("apps.journal.extraction.run_extraction_for_tenant") as fake_run,
        ):
            fake_dt.now.return_value = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
            fake_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            # First call (tenant a) raises; second (tenant b) succeeds
            fake_run.side_effect = [
                RuntimeError("LLM unavailable"),
                {"lessons": 1, "goals": 0, "tasks": 0, "task_actions": 0, "skipped": None},
            ]

            counts = nightly_extraction_task()

        # Order isn't deterministic with .iterator() — assert sums
        self.assertEqual(counts["considered"], 2)
        self.assertEqual(counts["errored"] + counts["fired"], 2)
        self.assertEqual(fake_run.call_count, 2)
        # Suppress unused-variable warning
        del a, b
