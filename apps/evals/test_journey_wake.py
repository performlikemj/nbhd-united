"""Tests for Probe 4 — hibernation-wake journey canary (PR-B4).

The whole point of this probe is the THREE HARD GATES, and each has a test that
proves it (docs/evals-wave-b-plan.md Probe 4). A wake probe that skips any of
them reads green while proving nothing:

  Gate 1 — could not ground-truth hibernate → run FAILS, never a skip
           (``test_could_not_hibernate_fails_not_skip``).
  Gate 2 — ready reply with ``waking_at`` never set (the WARM path) → run FAILS
           even though status==ready (``test_warm_path_fails_even_when_ready`` —
           THE critical test; without it the probe passes on a tenant that was
           never asleep).
  Gate 3 — ``waking_at`` seen but the turn terminates in ERROR / never terminal →
           run FAILS (``test_waking_seen_but_terminal_error_fails``,
           ``test_timeout_fails``).

The HTTP/poll layer and Azure are mocked; the ObservedTurn (from PR-B1's driver)
and the HibernateResult (from the force-hibernate wrapper) are injected so the
tests exercise the ASSERTION CHAIN, not the network.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.evals.journey.chat_drive import ObservedTurn
from apps.evals.journey.targets import JourneyConfigError
from apps.evals.journey.wake_control import HibernateResult, force_hibernate_and_confirm
from apps.evals.models import EvalRun
from apps.evals.runner import _assert_details_safe
from apps.evals.suites.journey_wake import (
    CASE_FLAGS,
    CASE_HIBERNATED,
    CASE_WAKE,
    CASE_WAKE_BUDGET_CAPPED,
    SLO_MS,
    WakeOutcome,
    classify_wake,
    run_wake_suite,
)
from apps.tenants.models import Tenant, User
from apps.tenants.pat_models import PersonalAccessToken, generate_pat


def _observed(**kw) -> ObservedTurn:
    """An ObservedTurn with the common terminal defaults; override per test."""
    base = {"client_msg_id": "x", "http_ok": True, "terminal": True}
    base.update(kw)
    return ObservedTurn(**base)


# A realistic in-SLO cold wake (2 min) and an over-SLO one.
_WAKE_120S_MS = 120_000
_WAKE_OVER_SLO_MS = SLO_MS + 5_000


# --------------------------------------------------------------------------- #
# classify_wake — the assertion logic (pure, no DB/HTTP). Order is load-bearing.
# --------------------------------------------------------------------------- #
class ClassifyWakeTest(TestCase):
    def test_hibernated_woke_ready_within_slo_is_pass(self):
        o = _observed(status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=_WAKE_120S_MS)
        self.assertEqual(classify_wake(o), WakeOutcome.PASS)

    def test_ready_without_waking_at_is_warm_path(self):
        # GATE 2, pure form: ready + tenant + no error + within SLO, but waking_at
        # was NEVER set → the WARM path ran (tenant was not asleep). NOT a pass.
        o = _observed(status="ready", source="tenant", error="", waking_at_seen=False, round_trip_ms=3_000)
        self.assertEqual(classify_wake(o), WakeOutcome.WARM_PATH)
        self.assertNotEqual(classify_wake(o), WakeOutcome.PASS)

    def test_ready_over_slo_is_slo_breach(self):
        o = _observed(status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=_WAKE_OVER_SLO_MS)
        self.assertEqual(classify_wake(o), WakeOutcome.SLO_BREACH)

    def test_terminal_error_with_waking_at_is_pipeline_error(self):
        # GATE 3, pure form: the wake started (waking_at seen) but the turn flipped
        # to ERROR — a stuck/failed wake, not a proven reply. waking_at seen does
        # NOT rescue it.
        o = _observed(status="error", error="empty_response", source="tenant", waking_at_seen=True)
        self.assertEqual(classify_wake(o), WakeOutcome.PIPELINE_ERROR)
        self.assertNotEqual(classify_wake(o), WakeOutcome.PASS)

    def test_never_terminal_is_timeout(self):
        o = _observed(terminal=False, timed_out=True, status="pending", waking_at_seen=True)
        self.assertEqual(classify_wake(o), WakeOutcome.TIMEOUT)

    def test_budget_exhausted_is_soft_not_warm_path(self):
        # Budget cap trips PRE-turn (no container work), so waking_at null is
        # EXPECTED — it must classify SOFT, not as a hard WARM_PATH failure.
        o = _observed(status="error", error="budget_exhausted", source="tenant", waking_at_seen=False)
        outcome = classify_wake(o)
        self.assertEqual(outcome, WakeOutcome.BUDGET_EXHAUSTED)
        self.assertNotEqual(outcome, WakeOutcome.WARM_PATH)
        self.assertNotEqual(outcome, WakeOutcome.PIPELINE_ERROR)

    def test_wrong_source_is_not_pass(self):
        o = _observed(status="ready", source="on_device", error="", waking_at_seen=True, round_trip_ms=1_000)
        self.assertEqual(classify_wake(o), WakeOutcome.WRONG_SOURCE)

    def test_http_failure_is_pipeline_error(self):
        o = _observed(http_ok=False, terminal=False, failure_stage="post")
        self.assertEqual(classify_wake(o), WakeOutcome.PIPELINE_ERROR)

    def test_ready_without_round_trip_is_pipeline_error(self):
        o = _observed(status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=None)
        self.assertEqual(classify_wake(o), WakeOutcome.PIPELINE_ERROR)

    def test_ready_with_nonempty_error_is_pipeline_error(self):
        o = _observed(status="ready", source="tenant", error="weird", waking_at_seen=True, round_trip_ms=100)
        self.assertEqual(classify_wake(o), WakeOutcome.PIPELINE_ERROR)


# --------------------------------------------------------------------------- #
# force_hibernate_and_confirm — the wrapper. Azure is the ground truth, not the
# DB flag; a revision that stays active is a real precondition failure.
# --------------------------------------------------------------------------- #
def _mock_tenant(*, container_id="oc-eval-journey", hibernated_at=None):
    t = MagicMock()
    t.id = "13fa39df-1111-2222-3333-444455556666"
    t.container_id = container_id
    t.hibernated_at = hibernated_at
    # refresh_from_db must NOT overwrite the controlled hibernated_at.
    t.refresh_from_db = MagicMock()
    return t


class ForceHibernateAndConfirmTest(TestCase):
    def test_confirms_on_zero_active_revisions(self):
        with (
            patch("apps.evals.journey.wake_control.hibernate_idle_tenant", return_value=True) as mock_hib,
            patch("apps.evals.journey.wake_control.container_app_has_active_revision", return_value=False),
        ):
            result = force_hibernate_and_confirm(_mock_tenant(), sleep=MagicMock())
        self.assertTrue(result.hibernated)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.failure_stage, "")
        mock_hib.assert_called_once()

    def test_confirms_after_azure_settles(self):
        # First revision read still shows active (list lag), second reads clear —
        # confirmed within a single force-hibernate attempt after one poll sleep.
        sleep_mock = MagicMock()
        with (
            patch("apps.evals.journey.wake_control.hibernate_idle_tenant", return_value=True) as mock_hib,
            patch(
                "apps.evals.journey.wake_control.container_app_has_active_revision",
                side_effect=[True, False],
            ),
        ):
            result = force_hibernate_and_confirm(_mock_tenant(), sleep=sleep_mock)
        self.assertTrue(result.hibernated)
        self.assertEqual(result.attempts, 1)
        mock_hib.assert_called_once()
        sleep_mock.assert_called_once()  # slept once between the two reads

    def test_retries_then_fails_when_revision_stays_active(self):
        # Something keeps waking it — a revision never goes inactive. After the
        # bounded retry this is a real FAILURE (never a skip), stage azure_active.
        with (
            patch("apps.evals.journey.wake_control.hibernate_idle_tenant", return_value=True) as mock_hib,
            patch("apps.evals.journey.wake_control.container_app_has_active_revision", return_value=True),
        ):
            result = force_hibernate_and_confirm(_mock_tenant(), max_attempts=2, confirm_polls=2, sleep=MagicMock())
        self.assertFalse(result.hibernated)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.failure_stage, "azure_active")
        self.assertEqual(mock_hib.call_count, 2)

    def test_hibernate_call_exception_is_failure_stage(self):
        # hibernate_idle_tenant itself raising (Azure error) is a hibernate_call
        # failure — the Azure confirm is never reached on that attempt.
        with (
            patch(
                "apps.evals.journey.wake_control.hibernate_idle_tenant",
                side_effect=RuntimeError("azure boom"),
            ),
            patch("apps.evals.journey.wake_control.container_app_has_active_revision") as mock_active,
        ):
            result = force_hibernate_and_confirm(_mock_tenant(), max_attempts=2, sleep=MagicMock())
        self.assertFalse(result.hibernated)
        self.assertEqual(result.failure_stage, "hibernate_call")
        mock_active.assert_not_called()

    def test_flag_set_reports_db_drift(self):
        # Azure says a revision is still active (not hibernated) while the DB flag
        # says hibernated — the drift the plan warns about. hibernated=False (Azure
        # ground truth) but flag_set=True (the DB datapoint) is recorded honestly.
        tenant = _mock_tenant(hibernated_at=timezone.now())
        with (
            patch("apps.evals.journey.wake_control.hibernate_idle_tenant", return_value=True),
            patch("apps.evals.journey.wake_control.container_app_has_active_revision", return_value=True),
        ):
            result = force_hibernate_and_confirm(tenant, max_attempts=1, confirm_polls=1, sleep=MagicMock())
        self.assertFalse(result.hibernated)
        self.assertTrue(result.flag_set)

    def test_wall_clock_budget_exceeded_is_clean_fail(self):
        # A wedged container: hibernate_idle_tenant's gateway calls burn the wall
        # clock. Once elapsed exceeds the budget, the wrapper stops BEFORE the
        # Azure confirm (and before the caller would start the ~240s drive) —
        # returning a clean Gate-1 FAIL rather than SIGKILLing the worker at 300s.
        clock = MagicMock(side_effect=[0.0, 0.0, 100.0, 100.0, 100.0])  # post-hibernate read jumps past budget
        with (
            patch("apps.evals.journey.wake_control.hibernate_idle_tenant", return_value=True) as mock_hib,
            patch("apps.evals.journey.wake_control.container_app_has_active_revision") as mock_active,
        ):
            result = force_hibernate_and_confirm(
                _mock_tenant(hibernated_at=timezone.now()), budget_seconds=45.0, sleep=MagicMock(), monotonic=clock
            )
        self.assertFalse(result.hibernated)
        self.assertEqual(result.failure_stage, "budget_exceeded")
        self.assertEqual(result.attempts, 1)
        mock_hib.assert_called_once()
        mock_active.assert_not_called()  # never reached the confirm — no drive would follow

    def test_transient_arm_blip_during_poll_is_absorbed(self):
        # A single ARM read raising is treated as a FAILED poll (still active), not
        # propagated — the next read confirms. No crash, no page.
        with (
            patch("apps.evals.journey.wake_control.hibernate_idle_tenant", return_value=True),
            patch(
                "apps.evals.journey.wake_control.container_app_has_active_revision",
                side_effect=[RuntimeError("arm blip"), False],
            ),
        ):
            result = force_hibernate_and_confirm(_mock_tenant(hibernated_at=timezone.now()), sleep=MagicMock())
        self.assertTrue(result.hibernated)
        self.assertEqual(result.attempts, 1)

    def test_sustained_arm_outage_is_not_confirmed(self):
        # Every ARM read raises → never confirmed → azure_active FAIL (not an
        # uncaught ERROR/crash). The confirm poll swallows the raises internally.
        with (
            patch("apps.evals.journey.wake_control.hibernate_idle_tenant", return_value=True),
            patch(
                "apps.evals.journey.wake_control.container_app_has_active_revision",
                side_effect=RuntimeError("arm down"),
            ),
        ):
            result = force_hibernate_and_confirm(_mock_tenant(), max_attempts=1, confirm_polls=2, sleep=MagicMock())
        self.assertFalse(result.hibernated)
        self.assertEqual(result.failure_stage, "azure_active")


# --------------------------------------------------------------------------- #
# run_wake_suite — recording + run-status wiring (hibernate + drive mocked).
# --------------------------------------------------------------------------- #
def _synthetic_tenant_with_pat() -> tuple[Tenant, str]:
    email = f"{secrets.token_hex(4)}@e.com"
    user = User.objects.create_user(username=email, email=email)
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        is_synthetic=True,
        is_eval_sink=True,
    )
    raw, prefix, token_hash = generate_pat()
    PersonalAccessToken.objects.create(user=user, name="eval-journey", token_prefix=prefix, token_hash=token_hash)
    return tenant, raw


_HIB_OK = HibernateResult(hibernated=True, attempts=1, flag_set=True, failure_stage="")
_HIB_FAIL = HibernateResult(hibernated=False, attempts=2, flag_set=False, failure_stage="azure_active")
# Azure confirmed 0 active revisions, but the DB flag was NOT stamped (a
# client-side hibernate timeout): container down + hibernated_at null = the brick
# state. Gate 1 must FAIL this, not pass on Azure alone.
_HIB_AZURE_ONLY = HibernateResult(hibernated=True, attempts=1, flag_set=False, failure_stage="")


class RunWakeSuiteTest(TestCase):
    def setUp(self):
        self.tenant, self.pat = _synthetic_tenant_with_pat()
        # Simulate the post-wake DB state so the flag cross-check passes on a real
        # wake (a genuine wake clears hibernated_at and stamps last_wake_at).
        self._set_flags(hibernated_at=None, last_wake_at=timezone.now())

    def _set_flags(self, *, hibernated_at, last_wake_at):
        Tenant.objects.filter(id=self.tenant.id).update(hibernated_at=hibernated_at, last_wake_at=last_wake_at)

    def _settings(self):
        return override_settings(
            EVAL_JOURNEY_TENANT_ID=str(self.tenant.id),
            EVAL_JOURNEY_PAT=self.pat,
            DJANGO_BASE_URL="https://cp.test",
        )

    def _run(self, hib: HibernateResult, observed: ObservedTurn):
        with (
            self._settings(),
            patch("apps.evals.suites.journey_wake.force_hibernate_and_confirm", return_value=hib),
            patch("apps.evals.suites.journey_wake.drive_chat_turn", return_value=observed) as drive,
        ):
            run = run_wake_suite(trigger=EvalRun.Trigger.MANUAL)
        return run, drive

    # ----- Gate 4 / happy chain --------------------------------------------- #
    def test_happy_chain_passes(self):
        observed = _observed(
            status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=_WAKE_120S_MS
        )
        run, drive = self._run(_HIB_OK, observed)
        self.assertEqual(run.status, EvalRun.Status.PASS)
        drive.assert_called_once()
        case_ids = {r.case_id for r in run.results.all()}
        self.assertEqual(case_ids, {CASE_HIBERNATED, CASE_WAKE, CASE_FLAGS})
        wake = run.results.get(case_id=CASE_WAKE)
        self.assertTrue(wake.passed)
        self.assertEqual(int(wake.score), _WAKE_120S_MS)
        self.assertEqual(int(wake.threshold), SLO_MS)

    # ----- Gate 2 — THE critical test --------------------------------------- #
    def test_warm_path_fails_even_when_ready(self):
        # A fast, clean ``ready`` reply from the tenant — but waking_at was NEVER
        # set, so the WARM path ran on a tenant that was not actually asleep. The
        # run MUST FAIL: without this the probe passes without exercising a wake.
        observed = _observed(status="ready", source="tenant", error="", waking_at_seen=False, round_trip_ms=2_000)
        run, drive = self._run(_HIB_OK, observed)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        drive.assert_called_once()
        wake = run.results.get(case_id=CASE_WAKE)
        self.assertFalse(wake.passed)
        self.assertEqual(wake.details["outcome"], WakeOutcome.WARM_PATH)
        self.assertFalse(wake.details["waking_at_seen"])

    # ----- Gate 1 — precondition failure is a FAIL, not a skip -------------- #
    def test_could_not_hibernate_fails_not_skip(self):
        observed = _observed(status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=1_000)
        run, drive = self._run(_HIB_FAIL, observed)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        # The message is NEVER sent when the tenant would not stay asleep —
        # driving it would exercise the warm path and prove nothing.
        drive.assert_not_called()
        results = list(run.results.all())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].case_id, CASE_HIBERNATED)
        self.assertFalse(results[0].passed)
        self.assertFalse(results[0].details["hibernated_confirmed"])

    def test_hibernated_but_flag_null_fails_not_skip(self):
        # Azure confirmed the container down, but hibernated_at is null (a
        # client-side hibernate timeout). Gating on Azure alone would drive a
        # message that 404s into a bricked container and misattribute it as a
        # broken wake. Requiring flag_set makes it a clean Gate-1 FAIL, message
        # never sent.
        observed = _observed(status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=1_000)
        run, drive = self._run(_HIB_AZURE_ONLY, observed)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        drive.assert_not_called()
        results = list(run.results.all())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].case_id, CASE_HIBERNATED)
        self.assertFalse(results[0].passed)
        self.assertTrue(results[0].details["hibernated_confirmed"])  # Azure said yes
        self.assertFalse(results[0].details["flag_set"])  # but the flag did not stamp — the brick state

    # ----- Gate 3 — waking seen but terminal error ------------------------- #
    def test_waking_seen_but_terminal_error_fails(self):
        observed = _observed(status="error", error="empty_response", source="tenant", waking_at_seen=True)
        run, _ = self._run(_HIB_OK, observed)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        wake = run.results.get(case_id=CASE_WAKE)
        self.assertFalse(wake.passed)
        self.assertEqual(wake.details["outcome"], WakeOutcome.PIPELINE_ERROR)

    def test_timeout_fails(self):
        observed = _observed(terminal=False, timed_out=True, status="pending", waking_at_seen=True)
        run, _ = self._run(_HIB_OK, observed)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(run.results.get(case_id=CASE_WAKE).details["outcome"], WakeOutcome.TIMEOUT)

    def test_slo_breach_fails_with_score(self):
        observed = _observed(
            status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=_WAKE_OVER_SLO_MS
        )
        run, _ = self._run(_HIB_OK, observed)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        wake = run.results.get(case_id=CASE_WAKE)
        self.assertFalse(wake.passed)
        self.assertEqual(int(wake.score), _WAKE_OVER_SLO_MS)
        self.assertEqual(int(wake.threshold), SLO_MS)

    def test_wrong_source_fails(self):
        observed = _observed(status="ready", source="on_device", error="", waking_at_seen=True, round_trip_ms=1_000)
        run, _ = self._run(_HIB_OK, observed)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertEqual(run.results.get(case_id=CASE_WAKE).details["outcome"], WakeOutcome.WRONG_SOURCE)

    # ----- Soft budget cap -------------------------------------------------- #
    def test_budget_exhausted_is_soft_pass_under_own_case_id(self):
        observed = _observed(status="error", error="budget_exhausted", source="tenant", waking_at_seen=False)
        run, _ = self._run(_HIB_OK, observed)
        # SOFT: no owner page. Recorded under the budget case id (never CASE_WAKE),
        # no score, and the flag cross-check is skipped (tenant stays hibernated).
        self.assertEqual(run.status, EvalRun.Status.PASS)
        case_ids = {r.case_id for r in run.results.all()}
        self.assertEqual(case_ids, {CASE_HIBERNATED, CASE_WAKE_BUDGET_CAPPED})
        self.assertNotIn(CASE_WAKE, case_ids)
        self.assertNotIn(CASE_FLAGS, case_ids)
        self.assertIsNone(run.results.get(case_id=CASE_WAKE_BUDGET_CAPPED).score)

    # ----- Secondary cross-check ------------------------------------------- #
    def test_flags_not_cleared_fails_the_run(self):
        # A clean ready+waking wake within SLO, but hibernated_at is still set —
        # a real inconsistency (a genuine wake clears it). The run must FAIL.
        self._set_flags(hibernated_at=timezone.now(), last_wake_at=timezone.now())
        observed = _observed(
            status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=_WAKE_120S_MS
        )
        run, _ = self._run(_HIB_OK, observed)
        self.assertEqual(run.status, EvalRun.Status.FAIL)
        self.assertTrue(run.results.get(case_id=CASE_WAKE).passed)  # the wake itself was fine
        flags = run.results.get(case_id=CASE_FLAGS)
        self.assertFalse(flags.passed)
        self.assertFalse(flags.details["hibernated_at_cleared"])

    # ----- INVARIANT #1 — details are metadata only ------------------------ #
    def test_details_are_metadata_only(self):
        observed = _observed(
            status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=_WAKE_120S_MS, polls=4
        )
        run, _ = self._run(_HIB_OK, observed)
        details = run.results.get(case_id=CASE_WAKE).details
        # record() would have raised via _assert_details_safe on anything unsafe;
        # re-assert here and check the shape is exactly codes/counts/durations.
        _assert_details_safe(details)
        self.assertEqual(details["outcome"], WakeOutcome.PASS)
        self.assertTrue(details["waking_at_seen"])
        self.assertEqual(details["round_trip_ms"], _WAKE_120S_MS)
        for value in details.values():
            self.assertIsInstance(value, (str, int, float, bool, type(None)))

    # ----- INVARIANT #3 — misconfiguration is loud ------------------------- #
    def test_unset_tenant_closes_error_and_raises(self):
        with (
            override_settings(EVAL_JOURNEY_TENANT_ID="", DJANGO_BASE_URL="https://cp.test", EVAL_JOURNEY_PAT=self.pat),
            self.assertRaises(JourneyConfigError),
        ):
            run_wake_suite(trigger=EvalRun.Trigger.MANUAL)
        self.assertEqual(EvalRun.objects.latest("started_at").status, EvalRun.Status.ERROR)


# --------------------------------------------------------------------------- #
# Task boundary — pass returns a summary; failure alerts owner + raises (DLQ).
# --------------------------------------------------------------------------- #
class EvalJourneyWakeTaskTest(TestCase):
    def setUp(self):
        self.tenant, self.pat = _synthetic_tenant_with_pat()
        Tenant.objects.filter(id=self.tenant.id).update(hibernated_at=None, last_wake_at=timezone.now())

    def _settings(self, **extra):
        base = {
            "EVAL_JOURNEY_TENANT_ID": str(self.tenant.id),
            "EVAL_JOURNEY_PAT": self.pat,
            "DJANGO_BASE_URL": "https://cp.test",
            "PLATFORM_OWNER_EMAIL": "owner@test.com",
            "EVAL_EMAIL_ALERTS_ENABLED": True,
        }
        base.update(extra)
        return override_settings(**base)

    def _run_task(self, observed: ObservedTurn):
        from apps.evals.tasks import eval_journey_wake_task

        with (
            self._settings(),
            patch("apps.evals.suites.journey_wake.force_hibernate_and_confirm", return_value=_HIB_OK),
            patch("apps.evals.suites.journey_wake.drive_chat_turn", return_value=observed),
        ):
            return eval_journey_wake_task()

    def test_task_passes_returns_summary(self):
        from django.core import mail

        observed = _observed(
            status="ready", source="tenant", error="", waking_at_seen=True, round_trip_ms=_WAKE_120S_MS
        )
        result = self._run_task(observed)
        self.assertEqual(result["status"], EvalRun.Status.PASS)
        self.assertEqual(result["suite"], "journey_wake")
        self.assertEqual(result["cases"], 3)
        self.assertEqual(len(mail.outbox), 0)

    def test_task_failure_alerts_owner_and_raises(self):
        from django.core import mail

        # Warm path (Gate 2) — the run FAILS, so the owner is alerted before the
        # DLQ raise.
        observed = _observed(status="ready", source="tenant", error="", waking_at_seen=False, round_trip_ms=2_000)
        with self.assertRaises(RuntimeError):
            self._run_task(observed)
        self.assertEqual(len(mail.outbox), 1)


class TaskMapTest(TestCase):
    def test_eval_journey_wake_registered_zero_arg(self):
        import inspect
        from importlib import import_module

        from apps.cron.views import TASK_MAP

        self.assertIn("eval_journey_wake", TASK_MAP)
        module_path, func_name = TASK_MAP["eval_journey_wake"].rsplit(".", 1)
        func = getattr(import_module(module_path), func_name)
        self.assertTrue(callable(func))
        inspect.signature(func).bind()  # zero-arg no-body-publish contract
