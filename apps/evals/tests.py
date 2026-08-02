"""Tests for the eval chassis (Wave A1): runner, smoke suite, task, TASK_MAP."""

from __future__ import annotations

from datetime import timedelta
from importlib import import_module

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import close_run, open_run, record, record_run


class RunnerChassisTest(TestCase):
    def test_all_pass_closes_pass_and_logs_summary(self):
        run = open_run("unit", EvalRun.Trigger.MANUAL, image_tag=None)
        record(run, "c1", EvalResult.Kind.CORPUS, passed=True)
        record(run, "c2", EvalResult.Kind.CORPUS, passed=True)

        with self.assertLogs("apps.evals.runner", level="INFO") as log:
            close_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertIsNotNone(run.finished_at)
        self.assertTrue(any("eval unit: PASS 2/2" in m for m in log.output))

    def test_any_failed_case_closes_fail_with_case_ids(self):
        run = open_run("unit", EvalRun.Trigger.MANUAL, image_tag=None)
        record(run, "ok", EvalResult.Kind.BEHAVIOR, passed=True)
        record(run, "bad", EvalResult.Kind.BEHAVIOR, passed=False, score=0.2, threshold=0.8)

        with self.assertLogs("apps.evals.runner", level="ERROR") as log:
            close_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        # The one summary line names the failed case id (and NOT the passed one's).
        joined = " ".join(log.output)
        self.assertIn("eval unit: FAIL 1/2", joined)
        self.assertIn("bad", joined)

    def test_zero_cases_closes_error_not_pass(self):
        run = open_run("unit", EvalRun.Trigger.MANUAL, image_tag=None)
        with self.assertLogs("apps.evals.runner", level="ERROR"):
            close_run(run)
        run.refresh_from_db()
        # A suite that asserted nothing must never read as a pass.
        self.assertEqual(run.status, EvalRun.Status.ERROR)

    def test_open_run_defaults_image_tag_for_runtime_suites(self):
        # Omitting image_tag infers the fleet tag; passing None stores NULL.
        with self.settings(OPENCLAW_IMAGE_TAG="oc-1.2.3-abc"):
            runtime = open_run("journey", EvalRun.Trigger.SCHEDULED)
            self.assertEqual(runtime.image_tag, "oc-1.2.3-abc")
        offline = open_run("corpus", EvalRun.Trigger.SCHEDULED, image_tag=None)
        self.assertIsNone(offline.image_tag)


class SmokeSuiteTest(TestCase):
    def test_smoke_writes_rows_and_passes(self):
        from apps.evals.suites.smoke import CASE_ID, SUITE, run_smoke_suite

        run = run_smoke_suite()

        self.assertEqual(run.suite, SUITE)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertIsNone(run.image_tag)  # smoke touches no container
        results = list(run.results.all())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].case_id, CASE_ID)
        self.assertTrue(results[0].passed)
        # details is metadata only — a duration, no content.
        self.assertEqual(set(results[0].details.keys()), {"duration_ms"})


class SmokeTaskTest(TestCase):
    def test_task_returns_summary_on_pass(self):
        from apps.evals.tasks import eval_smoke_task

        result = eval_smoke_task()
        self.assertEqual(result["status"], EvalRun.Status.PASS)
        self.assertEqual(result["suite"], "eval_smoke")
        self.assertEqual(result["cases"], 1)
        self.assertTrue(EvalRun.objects.filter(id=result["run_id"]).exists())

    def test_task_raises_when_run_does_not_pass(self):
        # If a case fails, the run closes 'fail' and the task must RAISE so QStash
        # DLQs it (never a silent green). Patch the suite's single case to fail.
        from unittest.mock import patch

        from apps.evals import runner
        from apps.evals.tasks import eval_smoke_task

        real_record = runner.record

        def failing_record(run, case_id, kind, passed, **kw):
            return real_record(run, case_id, kind, passed=False, **kw)

        with (
            patch("apps.evals.suites.smoke.record", side_effect=failing_record),
            self.assertRaises(RuntimeError),
        ):
            eval_smoke_task()


class TaskMapTest(TestCase):
    def test_eval_smoke_resolves_zero_arg(self):
        import inspect

        from apps.cron.views import TASK_MAP

        self.assertIn("eval_smoke", TASK_MAP)
        module_path, func_name = TASK_MAP["eval_smoke"].rsplit(".", 1)
        func = getattr(import_module(module_path), func_name)
        self.assertTrue(callable(func))
        inspect.signature(func).bind()  # zero-arg, no-body-publish contract


class DetailsChokepointTest(TestCase):
    """INVARIANT #1 enforcement at the record() chokepoint (Fable fix #2)."""

    def _run(self):
        return open_run("unit", EvalRun.Trigger.MANUAL, image_tag=None)

    def test_legit_metrics_dict_passes(self):
        r = record(
            self._run(),
            "case-1",
            EvalResult.Kind.BEHAVIOR,
            passed=True,
            details={"helpfulness": 4, "latency_ms": 4500, "tool": "cron.add", "failed_ids": ["c2", "c3"]},
        )
        self.assertEqual(r.details["tool"], "cron.add")

    def test_long_string_value_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            record(self._run(), "c", EvalResult.Kind.BEHAVIOR, passed=True, details={"rationale": "x" * 200})
        # The guard must report the LENGTH, never echo the offending string.
        msg = str(ctx.exception)
        self.assertIn("200 chars", msg)
        self.assertNotIn("x" * 65, msg)

    def test_non_scalar_leaf_is_rejected(self):
        with self.assertRaises(ValueError):
            record(self._run(), "c", EvalResult.Kind.BEHAVIOR, passed=True, details={"obj": {1, 2, 3}})

    def test_too_deep_nesting_is_rejected(self):
        with self.assertRaises(ValueError):
            record(self._run(), "c", EvalResult.Kind.BEHAVIOR, passed=True, details={"a": {"b": {"c": 1}}})

    def test_case_id_newlines_stripped_and_capped(self):
        r = record(self._run(), "case\nwith\rnewlines" + "-" * 100, EvalResult.Kind.SMOKE, passed=True)
        self.assertNotIn("\n", r.case_id)
        self.assertNotIn("\r", r.case_id)
        self.assertLessEqual(len(r.case_id), 64)


class RecordRunContextTest(TestCase):
    """record_run always closes the run — no stranded 'running' (Fable fix #5)."""

    def test_clean_block_closes_pass(self):
        with record_run("unit", EvalRun.Trigger.MANUAL, image_tag=None) as run:
            record(run, "c1", EvalResult.Kind.SMOKE, passed=True)
        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertIsNotNone(run.finished_at)

    def test_exception_closes_error_then_reraises(self):
        captured = {}
        with self.assertRaises(KeyError), record_run("unit", EvalRun.Trigger.MANUAL, image_tag=None) as run:
            captured["id"] = run.id
            raise KeyError("boom mid-suite")
        stranded = EvalRun.objects.get(id=captured["id"])
        # Fail closed: the crash left an 'error' run, never a phantom 'running'.
        self.assertEqual(stranded.status, EvalRun.Status.ERROR)
        self.assertIsNotNone(stranded.finished_at)


class ReapStuckRunsTest(TestCase):
    """Wave B5 reaper: flip orphaned 'running' runs to 'error', never a live one.

    ``started_at`` is ``auto_now_add`` so it can't be set on create — every test
    backdates it via a subsequent ``.update()`` (which does NOT re-stamp it) to
    simulate a run that opened N minutes ago.
    """

    def _running_run(self, *, minutes_ago: int) -> EvalRun:
        run = open_run("journey", EvalRun.Trigger.SCHEDULED, image_tag=None)
        # open_run leaves it 'running' with finished_at NULL; backdate started_at.
        EvalRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(minutes=minutes_ago))
        run.refresh_from_db()
        return run

    def test_backdated_running_run_is_reaped(self):
        from apps.evals.tasks import reap_stuck_eval_runs_task

        run = self._running_run(minutes_ago=31)  # past the 30-min floor

        result = reap_stuck_eval_runs_task()

        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.ERROR)
        self.assertIsNotNone(run.finished_at)  # finished_at stamped on reap
        self.assertEqual(result["reaped"], 1)
        self.assertEqual(result["run_ids"], [run.id])

    def test_fresh_running_run_is_untouched(self):
        # THE safety property: a run legitimately in flight (younger than the
        # 30-min floor) must NEVER be reaped — killing a live run is worse than
        # leaving a dead one stranded a little longer.
        from apps.evals.tasks import reap_stuck_eval_runs_task

        run = self._running_run(minutes_ago=1)  # well inside any live deadline

        result = reap_stuck_eval_runs_task()

        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.RUNNING)  # left running
        self.assertIsNone(run.finished_at)  # never stamped
        self.assertEqual(result["reaped"], 0)
        self.assertEqual(result["run_ids"], [])

    def test_boundary_run_just_under_floor_is_untouched(self):
        # A run 29 minutes old is still inside the floor — not reaped. Pins the
        # comparison to the cutoff so the floor can't silently drift to "reap
        # anything not brand-new."
        from apps.evals.tasks import reap_stuck_eval_runs_task

        run = self._running_run(minutes_ago=29)

        reap_stuck_eval_runs_task()

        run.refresh_from_db()
        self.assertEqual(run.status, EvalRun.Status.RUNNING)

    def test_completed_runs_are_untouched(self):
        # Already-terminal runs (pass / error) are never re-touched, even when
        # older than the floor — the reaper only acts on 'running'.
        from apps.evals.tasks import reap_stuck_eval_runs_task

        passed = self._running_run(minutes_ago=90)
        errored = self._running_run(minutes_ago=90)
        passed_finished = timezone.now() - timedelta(minutes=89)
        errored_finished = timezone.now() - timedelta(minutes=88)
        EvalRun.objects.filter(pk=passed.pk).update(status=EvalRun.Status.PASS, finished_at=passed_finished)
        EvalRun.objects.filter(pk=errored.pk).update(status=EvalRun.Status.ERROR, finished_at=errored_finished)

        result = reap_stuck_eval_runs_task()

        passed.refresh_from_db()
        errored.refresh_from_db()
        self.assertEqual(passed.status, EvalRun.Status.PASS)
        self.assertEqual(passed.finished_at, passed_finished)  # not re-stamped
        self.assertEqual(errored.status, EvalRun.Status.ERROR)
        self.assertEqual(errored.finished_at, errored_finished)
        self.assertEqual(result["reaped"], 0)

    def test_mixed_fleet_reaps_only_the_dead(self):
        # The realistic case: one stranded run beside a live one. Only the dead is
        # reaped; the live one keeps running; the count/ids reflect exactly that.
        from apps.evals.tasks import reap_stuck_eval_runs_task

        dead = self._running_run(minutes_ago=45)
        live = self._running_run(minutes_ago=2)

        with self.assertLogs("apps.evals.tasks", level="ERROR") as log:
            result = reap_stuck_eval_runs_task()

        dead.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(dead.status, EvalRun.Status.ERROR)
        self.assertEqual(live.status, EvalRun.Status.RUNNING)
        self.assertEqual(result["reaped"], 1)
        self.assertEqual(result["run_ids"], [dead.id])
        # The reaped count is logged for greppability in Log Analytics.
        self.assertTrue(any("flipped 1 orphaned eval run" in m for m in log.output))

    @override_settings(EVAL_EMAIL_ALERTS_ENABLED=True, PLATFORM_OWNER_EMAIL="owner@test.com")
    def test_reaper_batches_all_runs_into_one_email(self):
        from apps.evals.tasks import reap_stuck_eval_runs_task

        first = self._running_run(minutes_ago=45)
        second = self._running_run(minutes_ago=46)
        mail.outbox = []

        result = reap_stuck_eval_runs_task()

        self.assertEqual(result["reaped"], 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2 stuck eval run", mail.outbox[0].subject)
        self.assertIn(str(first.id), mail.outbox[0].body)
        self.assertIn(str(second.id), mail.outbox[0].body)

    @override_settings(EVAL_EMAIL_ALERTS_ENABLED=False, PLATFORM_OWNER_EMAIL="owner@test.com")
    def test_reaper_email_is_muted_without_burning_cooldown(self):
        from apps.evals.tasks import reap_stuck_eval_runs_task
        from apps.steward.models import AlertState

        self._running_run(minutes_ago=45)
        mail.outbox = []

        result = reap_stuck_eval_runs_task()

        self.assertEqual(result["reaped"], 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(AlertState.objects.filter(fingerprint="eval-email:reaper").exists())

    def test_reaper_registered_zero_arg_in_task_map(self):
        import inspect

        from apps.cron.views import TASK_MAP

        self.assertIn("reap_stuck_eval_runs", TASK_MAP)
        module_path, func_name = TASK_MAP["reap_stuck_eval_runs"].rsplit(".", 1)
        func = getattr(import_module(module_path), func_name)
        self.assertTrue(callable(func))
        inspect.signature(func).bind()  # zero-arg, no-body-publish contract
