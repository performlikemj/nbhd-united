"""Tests for the Wave B foundation: journey target resolver + failure alert + finalizer."""

from __future__ import annotations

import secrets

from django.core import mail
from django.test import TestCase, override_settings

from apps.evals.alerting import send_eval_failure_alert
from apps.evals.journey.targets import JourneyConfigError, resolve_journey_tenant
from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import open_run, record
from apps.evals.tasks import finalize_task_run
from apps.tenants.models import Tenant, User


def _tenant(*, synthetic: bool) -> Tenant:
    email = f"{secrets.token_hex(4)}@e.com"
    user = User.objects.create_user(username=email, email=email)
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        is_synthetic=synthetic,
        is_eval_sink=synthetic,
    )


class ResolveJourneyTenantTest(TestCase):
    def test_unset_raises(self):
        with override_settings(EVAL_JOURNEY_TENANT_ID=""), self.assertRaises(JourneyConfigError):
            resolve_journey_tenant()

    def test_malformed_id_raises(self):
        with override_settings(EVAL_JOURNEY_TENANT_ID="not-a-uuid"), self.assertRaises(JourneyConfigError):
            resolve_journey_tenant()

    def test_missing_tenant_raises(self):
        missing = "00000000-0000-0000-0000-000000000000"
        with override_settings(EVAL_JOURNEY_TENANT_ID=missing), self.assertRaises(JourneyConfigError):
            resolve_journey_tenant()

    def test_non_eval_sink_tenant_raises(self):
        real = _tenant(synthetic=False)
        with override_settings(EVAL_JOURNEY_TENANT_ID=str(real.id)), self.assertRaises(JourneyConfigError):
            resolve_journey_tenant()

    def test_synthetic_demo_tenant_raises(self):
        demo = _tenant(synthetic=True)
        demo.is_eval_sink = False
        demo.save(update_fields=["is_eval_sink"])
        with override_settings(EVAL_JOURNEY_TENANT_ID=str(demo.id)), self.assertRaises(JourneyConfigError):
            resolve_journey_tenant()

    def test_eval_sink_tenant_resolves(self):
        synth = _tenant(synthetic=True)
        with override_settings(EVAL_JOURNEY_TENANT_ID=str(synth.id)):
            self.assertEqual(resolve_journey_tenant().id, synth.id)


class FailureAlertTest(TestCase):
    def _failed_run(self) -> EvalRun:
        run = open_run("journey", EvalRun.Trigger.SCHEDULED, image_tag="oc-1.2.3-abc")
        record(run, "probe-chat-roundtrip", EvalResult.Kind.JOURNEY, passed=True)
        # A failed case whose details carry a distinctive metadata value we can
        # prove NEVER reaches the email body (alert serializes no details).
        record(run, "probe-wake", EvalResult.Kind.JOURNEY, passed=False, details={"reason_code": "NEVER_IN_EMAIL"})
        run.status = EvalRun.Status.FAIL
        run.git_sha = "deadbeefdeadbeef"
        run.save(update_fields=["status", "git_sha"])
        return run

    @override_settings(PLATFORM_OWNER_EMAIL="")
    def test_skips_and_logs_when_owner_email_unset(self):
        with self.assertLogs("apps.evals.alerting", level="WARNING"):
            sent = send_eval_failure_alert(self._failed_run())
        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(PLATFORM_OWNER_EMAIL="owner@test.com")
    def test_sends_content_free_metadata_email(self):
        run = self._failed_run()
        sent = send_eval_failure_alert(run)
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["owner@test.com"])
        # Content-safe metadata IS present:
        self.assertIn("journey", msg.subject)
        self.assertIn("1/2", msg.subject)  # passed/total
        self.assertIn("probe-wake", msg.body)  # failed case id
        self.assertIn("deadbeefdeadbeef", msg.body)  # git sha
        self.assertIn("oc-1.2.3-abc", msg.body)  # image tag
        # Content-free: EvalResult.details values NEVER reach the email.
        self.assertNotIn("NEVER_IN_EMAIL", msg.body)
        self.assertNotIn("reason_code", msg.body)


class FinalizeTaskRunTest(TestCase):
    @override_settings(PLATFORM_OWNER_EMAIL="owner@test.com")
    def test_pass_run_is_noop(self):
        run = open_run("journey", EvalRun.Trigger.SCHEDULED, image_tag=None)
        record(run, "c1", EvalResult.Kind.JOURNEY, passed=True)
        run.status = EvalRun.Status.PASS
        run.save(update_fields=["status"])
        finalize_task_run(run)  # no raise
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(PLATFORM_OWNER_EMAIL="owner@test.com")
    def test_nonpass_run_alerts_then_raises(self):
        run = open_run("journey", EvalRun.Trigger.SCHEDULED, image_tag=None)
        record(run, "c1", EvalResult.Kind.JOURNEY, passed=False)
        run.status = EvalRun.Status.FAIL
        run.save(update_fields=["status"])
        with self.assertRaises(RuntimeError):
            finalize_task_run(run)
        # Owner was alerted before the raise (best-effort, DLQ-visible failure).
        self.assertEqual(len(mail.outbox), 1)
