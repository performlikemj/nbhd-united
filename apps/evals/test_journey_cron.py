"""Tests for the Wave B cron-fire delivery canary (Probe 3, ``journey_cron``).

The OC-side firing is mocked (the gateway ``cron.add`` push) — these tests
exercise the two things that make this a TRUE eval and not green theater: the
delivery assertion (a fresh ``ProactiveOutbound`` with the unique per-run
``job_name`` inside the ``created_at`` window) and the arming logic (a one-shot
``at`` cron with a unique name). Explicitly proven here: a stale historical row
does NOT pass, and a REGISTERED-but-never-fired ``CronJob`` does NOT pass.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.cron.models import CronJob
from apps.evals.models import EvalResult, EvalRun
from apps.evals.suites.journey_cron import (
    SUITE,
    _observe_delivery,
    run_cron_fire_suite,
)
from apps.router.models import ProactiveOutbound
from apps.tenants.models import Tenant, User

_GATEWAY = "apps.cron.gateway_client.invoke_gateway_tool"


def _synthetic_tenant() -> Tenant:
    email = f"{secrets.token_hex(4)}@e.com"
    user = User.objects.create_user(username=email, email=email)
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, is_synthetic=True)


def _make_outbound(tenant, *, job_name: str, created_at=None) -> ProactiveOutbound:
    """Create a ProactiveOutbound row, optionally back-dating ``created_at``.

    ``created_at`` is ``auto_now_add`` so we override it with a queryset update
    (bypasses auto-stamp) to plant a stale historical row.
    """
    row = ProactiveOutbound.objects.create(
        tenant=tenant,
        channel=ProactiveOutbound.Channel.APP,
        channel_user_id=str(tenant.user_id),
        message_text="[reminder]",
        job_name=job_name,
    )
    if created_at is not None:
        ProactiveOutbound.objects.filter(pk=row.pk).update(created_at=created_at)
        row.refresh_from_db()
    return row


class ObserveDeliveryTest(TestCase):
    """The delivery assertion in isolation — no gateway, no timing flakiness."""

    def setUp(self):
        self.tenant = _synthetic_tenant()
        self.job_name = "eval-cron-abc123"
        self.window_start = timezone.now()

    def _observe(self):
        # deadline 0 + no-op sleep → exactly one poll, fully deterministic.
        return _observe_delivery(
            self.tenant,
            job_name=self.job_name,
            window_start=self.window_start,
            deadline_seconds=0,
            interval_seconds=0,
            sleep_fn=lambda _s: None,
        )

    def test_in_window_matching_row_is_detected(self):
        _make_outbound(self.tenant, job_name=self.job_name)  # created_at = now (>= window_start)
        self.assertTrue(self._observe().delivered)

    def test_stale_historical_row_does_not_pass(self):
        # Same job_name, but created BEFORE the window opened → must NOT count.
        stale = self.window_start - timedelta(hours=1)
        _make_outbound(self.tenant, job_name=self.job_name, created_at=stale)
        self.assertFalse(self._observe().delivered)

    def test_different_job_name_row_does_not_pass(self):
        # A concurrent unrelated cron's row is in-window but has a different name.
        _make_outbound(self.tenant, job_name="eval-cron-other999")
        self.assertFalse(self._observe().delivered)

    def test_other_tenant_row_does_not_pass(self):
        other = _synthetic_tenant()
        _make_outbound(other, job_name=self.job_name)
        self.assertFalse(self._observe().delivered)


@override_settings(PLATFORM_OWNER_EMAIL="")
class RunCronFireSuiteTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()
        self.settings_ctx = override_settings(EVAL_JOURNEY_TENANT_ID=str(self.tenant.id))
        self.settings_ctx.enable()
        self.addCleanup(self.settings_ctx.disable)

    def _armed_crons(self):
        return CronJob.objects.filter(tenant=self.tenant, name__startswith="eval-cron-")

    @patch(_GATEWAY)
    def test_delivered_row_makes_run_pass(self, mock_gw):
        mock_gw.return_value = {"details": {"id": "job-1"}}

        planted = {"done": False}

        def fake_sleep(_seconds):
            # Simulate OpenClaw firing the just-armed cron mid-poll: the delivery
            # view would write this row with the cron's name as job_name.
            if not planted["done"]:
                job = self._armed_crons().latest("created_at")
                _make_outbound(self.tenant, job_name=job.name)
                planted["done"] = True

        run = run_cron_fire_suite(
            trigger=EvalRun.Trigger.MANUAL,
            lead_seconds=1,
            deadline_seconds=10,
            interval_seconds=1,
            sleep_fn=fake_sleep,
        )
        self.assertEqual(run.status, EvalRun.Status.PASS)
        case = run.results.get()
        self.assertTrue(case.passed)
        self.assertEqual(case.kind, EvalResult.Kind.JOURNEY)

    @patch(_GATEWAY)
    def test_no_delivery_makes_run_fail(self, mock_gw):
        mock_gw.return_value = {"details": {"id": "job-1"}}
        run = run_cron_fire_suite(
            trigger=EvalRun.Trigger.MANUAL,
            lead_seconds=1,
            deadline_seconds=0,
            interval_seconds=0,
            sleep_fn=lambda _s: None,
        )
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertFalse(run.results.get().passed)

    @patch(_GATEWAY)
    def test_registered_cron_without_delivery_still_fails(self, mock_gw):
        """Anti-green-theater: a fully REGISTERED cron with no fire must FAIL.

        Proves the run asserts on ProactiveOutbound, not on CronJob registration
        fields (``enabled`` / ``last_synced_at``) which prove nothing about firing.
        """
        mock_gw.return_value = {"details": {"id": "job-1"}}
        run = run_cron_fire_suite(
            trigger=EvalRun.Trigger.MANUAL,
            lead_seconds=1,
            deadline_seconds=0,
            interval_seconds=0,
            sleep_fn=lambda _s: None,
        )
        # The cron IS registered (enabled) — stamp the sync markers to make the
        # point that even a "healthy registration" reads FAIL without a delivery.
        cron = self._armed_crons().get()
        now = timezone.now()
        CronJob.objects.filter(pk=cron.pk).update(enabled=True, last_synced_at=now, last_pushed_to_container_at=now)
        self.assertTrue(CronJob.objects.get(pk=cron.pk).enabled)  # registered...
        self.assertFalse(
            ProactiveOutbound.objects.filter(tenant=self.tenant, job_name=cron.name).exists()
        )  # ...but never fired
        self.assertEqual(run.status, EvalRun.Status.FAIL)  # so: FAIL

    @patch(_GATEWAY)
    def test_armed_cron_is_one_shot_with_unique_name(self, mock_gw):
        mock_gw.return_value = {"details": {"id": "job-1"}}
        kwargs = dict(
            trigger=EvalRun.Trigger.MANUAL,
            lead_seconds=1,
            deadline_seconds=0,
            interval_seconds=0,
            sleep_fn=lambda _s: None,
        )
        run_cron_fire_suite(**kwargs)
        run_cron_fire_suite(**kwargs)

        crons = list(self._armed_crons())
        self.assertEqual(len(crons), 2)
        # Distinct per-run names (so a stale row can never collide by name).
        self.assertEqual(len({c.name for c in crons}), 2)
        for c in crons:
            self.assertFalse(c.managed)  # one-shot at-crons are unmanaged
            self.assertEqual(c.data["schedule"]["kind"], "at")

    @patch(_GATEWAY)
    def test_details_are_metadata_only(self, mock_gw):
        mock_gw.return_value = {"details": {"id": "job-1"}}
        run = run_cron_fire_suite(
            trigger=EvalRun.Trigger.MANUAL,
            lead_seconds=1,
            deadline_seconds=0,
            interval_seconds=0,
            sleep_fn=lambda _s: None,
        )
        details = run.results.get().details
        # Every value is a count / flag — no strings, so no content can hide here.
        for key, value in details.items():
            self.assertIsInstance(value, (int, bool), f"details[{key!r}] is not metadata")
        self.assertIn("delivered", details)
        self.assertIn("armed", details)

    def test_misconfigured_target_closes_error(self):
        # Unset target → resolve_journey_tenant raises → run closes ERROR (loud),
        # never a silent pass (directive INVARIANT #3).
        with override_settings(EVAL_JOURNEY_TENANT_ID=""), self.assertRaises(Exception):
            run_cron_fire_suite(
                trigger=EvalRun.Trigger.MANUAL,
                deadline_seconds=0,
                interval_seconds=0,
                sleep_fn=lambda _s: None,
            )
        run = EvalRun.objects.filter(suite=SUITE).latest("started_at")
        self.assertEqual(run.status, EvalRun.Status.ERROR)
        self.assertEqual(run.results.count(), 0)
