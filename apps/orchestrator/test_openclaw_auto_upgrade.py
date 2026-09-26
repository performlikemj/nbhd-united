"""OpenClaw 5.28 -> 9.4 auto-upgrade at idle time.

Pins the directive's acceptance: the rules table maps every outcome, the
safe-exit guard never leaves a record fenced, the lease is single-flight,
cooldown is honoured, and Jev can only pick safe actions.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.orchestrator import openclaw_auto_upgrade as au
from apps.orchestrator import openclaw_migration
from apps.orchestrator.hibernation import check_cron_wake_idle_task
from apps.orchestrator.openclaw_migration import MigrationError, MigrationFailed
from apps.orchestrator.tasks import hibernate_idle_tenants_task
from apps.orchestrator.test_tenant_openclaw_migration import TAG, tenant_fixture
from apps.tenants.models import OpenClawAutoUpgrade, OpenClawAutoUpgradeLock, Tenant

ON = {
    "OPENCLAW_AUTO_UPGRADE_ENABLED": True,
    "OPENCLAW_AUTO_UPGRADE_TENANT_IDS": "*",
    "OPENCLAW_AUTO_UPGRADE_TAG": TAG,
    "PLATFORM_OWNER_EMAIL": "owner@example.invalid",
}


def failed(step, reason, status="FAILED"):
    return {"status": status, "step": step, "reason": reason, "reasons": {}}


class RulesTableTests(SimpleTestCase):
    def test_every_directive_outcome(self):
        cases = [
            ({"status": "PASS"}, {}, au.FINISH_PASS),
            ({"status": "NOOP"}, {}, au.FINISH_PASS),
            ({"status": "BLOCKED_UNSUPPORTED", "reasons": {"x": 1}}, {}, au.ROLLBACK_ALERT),
            (failed("crons", "BLOCKED_UNSUPPORTED x=1", "BLOCKED_UNSUPPORTED"), {}, au.ROLLBACK_ALERT),
            ({"status": "DEFER", "reasons": {"tenant_unavailable": 1}}, {}, au.REWAKE_RESUME),
            ({"status": "DEFER", "reasons": {"tenant_unavailable": 1}}, {"rewakes": 1}, au.GIVE_UP_QUIET),
            ({"status": "DEFER", "reasons": {"cron_imminent": 1}}, {}, au.GIVE_UP_QUIET),
            ({"status": "DEFER", "reasons": {"cron_running": 1}}, {}, au.GIVE_UP_QUIET),
            (failed("image", "cron_imminent", "DEFERRED"), {}, au.GIVE_UP_QUIET),
            (failed("image", "Bounded revision/proxy-health/healthz wait expired"), {}, au.ROLLBACK_ALERT),
            (failed("version", "migration_owner_fenced"), {}, au.ALERT_ONLY),
            (failed("version", "OperationalError"), {}, au.ROLLBACK_ALERT),
            (failed("config", "Operator command timed out or returned no result"), {}, au.ROLLBACK_ALERT),
            (failed("crons", "Operator command timed out or returned no result"), {}, au.WAIT_RESUME),
            (failed("verify", "Operator console failed (output withheld)"), {"transient_resumes": 1}, au.WAIT_RESUME),
            (
                failed("verify", "Operator console failed (output withheld)"),
                {"transient_resumes": 2},
                au.ROLLBACK_ALERT,
            ),
            (failed("crons", "HTTP 429 Too Many Requests"), {}, au.WAIT_RESUME),
            (
                failed("image", "Hibernated tenant refused: wake on its current image, then migrate"),
                {},
                au.REWAKE_RESUME,
            ),
            (
                failed("crons", "Migration requires an active, provisioned tenant"),
                {"rewakes": 1},
                au.ROLLBACK_ALERT,
            ),
            (failed("verify", "canonical_content_mismatch"), {}, au.CLASSIFY),
            (failed("preflight", "acr_digest_unavailable"), {}, au.CLASSIFY),
            (failed("capture", "SomethingNew"), {}, au.CLASSIFY),
            ({"status": "ERROR", "reason": "Migration already running; use --takeover"}, {}, au.GIVE_UP_QUIET),
            ({"status": "ERROR", "reason": "Existing migration targets another tag"}, {}, au.ALERT_ONLY),
            ({"status": "SURPRISE"}, {}, au.CLASSIFY),
        ]
        for outcome, counters, expected in cases:
            with self.subTest(outcome=outcome, counters=counters):
                self.assertEqual(au.decide(outcome, counters), expected)

    def test_post_swap_failures_never_retry(self):
        for step in ("image", "version", "config"):
            for reason in ("Operator console failed (output withheld)", "anything", "429"):
                with self.subTest(step=step, reason=reason):
                    self.assertEqual(au.decide(failed(step, reason), {}), au.ROLLBACK_ALERT)


class JevPolicyTests(SimpleTestCase):
    SAFE = {au.WAIT_RESUME, au.ROLLBACK_ALERT}

    def test_only_safe_actions_for_any_choice(self):
        for choice in list(au.JEV_CHOICES) + ["push_through", "skip_verify", ""]:
            for p in (0.0, 0.5, 0.849, 0.85, 1.0):
                for used in (0, 1):
                    with self.subTest(choice=choice, p=p, used=used):
                        action = au.jev_policy(choice, {choice: p, "transient_retry_later": p}, used=used)
                        self.assertIn(action, self.SAFE)

    def test_confident_transient_retries_once(self):
        probs = {"transient_retry_later": 0.9, "needs_rollback_now": 0.05, "needs_human": 0.05}
        self.assertEqual(au.jev_policy("transient_retry_later", probs, used=0), au.WAIT_RESUME)
        self.assertEqual(au.jev_policy("transient_retry_later", probs, used=1), au.ROLLBACK_ALERT)

    def test_low_confidence_and_human_fall_through_to_rollback(self):
        low = {"transient_retry_later": 0.6, "needs_rollback_now": 0.2, "needs_human": 0.2}
        self.assertEqual(au.jev_policy("transient_retry_later", low, used=0), au.ROLLBACK_ALERT)
        human = {"transient_retry_later": 0.05, "needs_rollback_now": 0.05, "needs_human": 0.9}
        self.assertEqual(au.jev_policy("needs_human", human, used=0), au.ROLLBACK_ALERT)

    @override_settings(OPENCLAW_AUTO_UPGRADE_JEV_ENABLED=False)
    def test_disabled_jev_is_never_called(self):
        with patch.object(au, "classify_with_jev") as classify:
            self.assertEqual(au._classify(failed("verify", "weird"), {}), (au.ROLLBACK_ALERT, None))
        classify.assert_not_called()

    @override_settings(OPENCLAW_AUTO_UPGRADE_JEV_ENABLED=True)
    def test_jev_unavailable_rolls_back(self):
        with patch.object(au, "classify_with_jev", return_value=None):
            action, verdict = au._classify(failed("verify", "weird"), {})
        self.assertEqual(action, au.ROLLBACK_ALERT)
        self.assertEqual(verdict, {"choice": "unavailable"})

    def test_state_is_scrubbed(self):
        text = "boom 4e13ec0e-08d8-4999-8070-d446475f29e4 oc-4e13ec0e-app deadbeefcafe " + "x" * 400
        scrubbed = au.scrub(text)
        self.assertNotIn("4e13ec0e", scrubbed)
        self.assertNotIn("deadbeefcafe", scrubbed)
        self.assertLessEqual(len(scrubbed), 240)

    @override_settings(OPENCLAW_AUTO_UPGRADE_JEV_ENABLED=True)
    def test_jev_request_is_a_three_way_choice_without_ids(self):
        from apps.common import jev

        seen = {}

        def fake_decide(state, questions, **kwargs):
            seen["state"], seen["questions"] = state, questions

            class Result:
                answers = {
                    "failure": jev.ChoiceAnswer(
                        type="choice",
                        choice="transient_retry_later",
                        probabilities={"transient_retry_later": 0.9, "needs_rollback_now": 0.05, "needs_human": 0.05},
                        confidence=0.9,
                    )
                }

            return Result()

        with patch.object(jev, "decide", side_effect=fake_decide):
            verdict = au.classify_with_jev("verify", "failed for 4e13ec0e-08d8-4999-8070-d446475f29e4", "FAILED")
        self.assertEqual(set(seen["questions"]["failure"].criteria), set(au.JEV_CHOICES))
        self.assertNotIn("4e13ec0e", str(seen["state"]))
        self.assertEqual(verdict["choice"], "transient_retry_later")


class LeaseTests(TestCase):
    def setUp(self):
        self.a = tenant_fixture(94001)
        self.b = tenant_fixture(94002)

    def test_single_flight(self):
        token = au.acquire(self.a)
        self.assertTrue(token)
        self.assertIsNone(au.acquire(self.b))
        self.assertTrue(au.holds(self.a.id, token))
        self.assertTrue(au.in_flight(self.a.id))
        self.assertFalse(au.in_flight(self.b.id))

    def test_release_spaces_the_next_run(self):
        token = au.acquire(self.a)
        au.release(token)
        self.assertIsNone(au.acquire(self.b))
        OpenClawAutoUpgradeLock.objects.filter(pk=1).update(next_allowed_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(au.acquire(self.b))

    def test_expired_holder_is_not_free_and_reaper_recovers_it(self):
        token = au.acquire(self.a)
        OpenClawAutoUpgradeLock.objects.filter(pk=1).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(au.acquire(self.b))
        with patch("apps.cron.publish.publish_task") as publish:
            self.assertEqual(au.reap_stale_run(), "recovering")
        row = OpenClawAutoUpgradeLock.objects.get(pk=1)
        self.assertNotEqual(row.run_token, token)
        self.assertEqual(row.tenant_id, self.a.id)
        publish.assert_called_once_with(au.TASK_NAME, str(self.a.id), row.run_token, "recover", retries=0)

    def test_live_lease_is_left_alone(self):
        au.acquire(self.a)
        with patch("apps.cron.publish.publish_task") as publish:
            self.assertIsNone(au.reap_stale_run())
        publish.assert_not_called()


@override_settings(**ON)
class EligibilityTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(94010)

    def test_eligible_5_28(self):
        self.assertTrue(au.eligible(self.tenant))

    def test_default_off_and_empty_allowlist(self):
        with override_settings(OPENCLAW_AUTO_UPGRADE_ENABLED=False):
            self.assertFalse(au.eligible(self.tenant))
        with override_settings(OPENCLAW_AUTO_UPGRADE_TENANT_IDS=""):
            self.assertFalse(au.eligible(self.tenant))
        with override_settings(OPENCLAW_AUTO_UPGRADE_TENANT_IDS=str(self.tenant.id)):
            self.assertTrue(au.eligible(self.tenant))
        with override_settings(OPENCLAW_AUTO_UPGRADE_TENANT_IDS="00000000-0000-0000-0000-000000000000"):
            self.assertFalse(au.eligible(self.tenant))

    def test_cooldown_is_honoured(self):
        OpenClawAutoUpgrade.objects.create(tenant=self.tenant, cooldown_until=timezone.now() + timedelta(days=6))
        self.assertFalse(au.eligible(self.tenant))
        OpenClawAutoUpgrade.objects.filter(tenant=self.tenant).update(cooldown_until=timezone.now() - timedelta(1))
        self.assertTrue(au.eligible(self.tenant))

    def test_out_of_scope_tenants(self):
        for field, value in (
            ("openclaw_version", "2026.4.25"),
            ("openclaw_version", "2026.9.4"),
            ("status", Tenant.Status.SUSPENDED),
            ("openclaw_migration_cron_fenced", True),
            ("openclaw_migration", {"status": "FAILED"}),
        ):
            with self.subTest(field=field, value=value):
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                setattr(tenant, field, value)
                self.assertFalse(au.eligible(tenant))
        self.tenant.openclaw_migration = {"status": "ROLLED_BACK"}
        self.assertTrue(au.eligible(self.tenant))

    def test_tag_must_be_immutable_9_4(self):
        with override_settings(OPENCLAW_AUTO_UPGRADE_TAG="", OPENCLAW_IMAGE_TAG="latest"):
            self.assertFalse(au.eligible(self.tenant))
        with override_settings(OPENCLAW_AUTO_UPGRADE_TAG="", OPENCLAW_IMAGE_TAG=TAG):
            self.assertTrue(au.eligible(self.tenant))


@override_settings(**ON, TENANT_IDLE_HIBERNATE_MINUTES=30)
class SweepHookTests(TestCase):
    def setUp(self):
        self.enterContext(patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}))
        self.hibernate = self.enterContext(
            patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
        )
        self.publish = self.enterContext(patch("apps.cron.publish.publish_task"))

    def idle(self, chat_id, version="2026.5.28"):
        tenant = tenant_fixture(chat_id)
        tenant.openclaw_version = version
        tenant.last_message_at = timezone.now() - timedelta(hours=3)
        tenant.save()
        return tenant

    def test_idle_5_28_tenant_upgrades_instead_of_hibernating(self):
        tenant = self.idle(94020)
        result = hibernate_idle_tenants_task()
        self.assertEqual(result["auto_upgrading"], 1)
        self.hibernate.assert_not_called()
        run_token = OpenClawAutoUpgradeLock.objects.get(pk=1).run_token
        self.publish.assert_called_once_with(au.TASK_NAME, str(tenant.id), run_token, "start", retries=0)

    def test_only_one_tenant_per_flight_others_hibernate(self):
        self.idle(94021)
        self.idle(94022)
        self.idle(94023, version="2026.9.4")
        result = hibernate_idle_tenants_task()
        self.assertEqual(result["auto_upgrading"], 1)
        self.assertEqual(self.hibernate.call_count, 2)

    def test_default_off_hibernates_as_before(self):
        with override_settings(OPENCLAW_AUTO_UPGRADE_ENABLED=False):
            self.idle(94024)
            result = hibernate_idle_tenants_task()
        self.assertEqual(result["auto_upgrading"], 0)
        self.assertEqual(self.hibernate.call_count, 1)
        self.publish.assert_not_called()

    def test_tenant_mid_upgrade_is_never_hibernated(self):
        tenant = self.idle(94025)
        au.acquire(tenant)
        with override_settings(OPENCLAW_AUTO_UPGRADE_ENABLED=False):
            result = hibernate_idle_tenants_task()
        self.assertEqual(result["auto_upgrading"], 1)
        self.hibernate.assert_not_called()

    def test_failed_publish_releases_the_lease(self):
        self.publish.side_effect = RuntimeError("qstash down")
        self.idle(94026)
        result = hibernate_idle_tenants_task()
        self.assertEqual(result["auto_upgrading"], 0)
        self.assertEqual(self.hibernate.call_count, 1)
        self.assertEqual(OpenClawAutoUpgradeLock.objects.get(pk=1).run_token, "")

    def test_cron_wake_idle_check_skips_tenant_mid_upgrade(self):
        tenant = self.idle(94027)
        Tenant.objects.filter(pk=tenant.pk).update(cron_wake_at=timezone.now() - timedelta(minutes=20))
        au.acquire(tenant)
        self.assertEqual(check_cron_wake_idle_task(str(tenant.id)), {"status": "auto_upgrade_in_flight"})
        self.hibernate.assert_not_called()


class SafeExitGuardTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(94030)

    def claim(self, **record):
        Tenant.objects.filter(pk=self.tenant.pk).update(
            openclaw_migration={"owner_token": "mine", "tag": TAG, **record}, openclaw_migration_cron_fenced=True
        )

    def test_pass_is_left_alone(self):
        self.claim(status="PASS")
        self.assertEqual(au.ensure_safe_exit(self.tenant, "mine"), "pass")

    def test_not_ours_is_left_alone(self):
        self.claim(status="FAILED")
        self.assertEqual(au.ensure_safe_exit(self.tenant, "other"), "not_ours")
        self.assertEqual(au.ensure_safe_exit(self.tenant, ""), "not_ours")
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.openclaw_migration_cron_fenced)

    def test_no_undo_point_resets_and_unfences(self):
        self.claim(status="FAILED", failure={"step": "preflight", "reason": "acr_digest_unavailable"})
        self.assertEqual(au.ensure_safe_exit(self.tenant, "mine"), "reset")
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.openclaw_migration_cron_fenced)
        self.assertEqual(self.tenant.openclaw_migration["status"], "ROLLED_BACK")
        self.assertEqual(self.tenant.openclaw_migration["previous"]["failure"]["step"], "preflight")
        self.assertEqual(self.tenant.openclaw_version, "2026.5.28")

    def test_undo_point_rolls_back(self):
        self.claim(status="FAILED", undo={"share_snapshot": "s"})

        def rollback(tenant_id):
            Tenant.objects.filter(pk=tenant_id).update(
                openclaw_migration={"status": "ROLLED_BACK"}, openclaw_migration_cron_fenced=False
            )
            return {"status": "ROLLED_BACK"}

        with patch.object(openclaw_migration, "rollback_tenant", side_effect=rollback) as rb:
            self.assertEqual(au.ensure_safe_exit(self.tenant, "mine"), "rolled_back")
        rb.assert_called_once_with(self.tenant.id)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.openclaw_migration_cron_fenced)

    def test_stuck_running_record_of_ours_is_failed_then_undone(self):
        self.claim(status="RUNNING", step="crons", lease_until=(timezone.now() + timedelta(minutes=9)).isoformat())
        self.assertEqual(au.ensure_safe_exit(self.tenant, "mine"), "reset")
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.openclaw_migration_cron_fenced)
        self.assertEqual(self.tenant.openclaw_migration["previous"]["failure"]["reason"], "owner_exited")

    def test_rollback_retried_once_then_reported(self):
        self.claim(status="FAILED", undo={"share_snapshot": "s"})
        with patch.object(
            openclaw_migration, "rollback_tenant", side_effect=MigrationError("Bounded wait expired")
        ) as rb:
            self.assertEqual(au.ensure_safe_exit(self.tenant, "mine"), "failed:Bounded wait expired")
        self.assertEqual(rb.call_count, 2)

    def test_reset_refuses_live_lease_and_undo_points(self):
        self.claim(status="RUNNING", lease_until=(timezone.now() + timedelta(minutes=5)).isoformat())
        with self.assertRaisesMessage(MigrationError, "migration_running"):
            openclaw_migration.reset_unsubmitted_migration(self.tenant.id)
        self.claim(status="FAILED", undo={"share_snapshot": "s"})
        with self.assertRaisesMessage(MigrationError, "undo_point_exists"):
            openclaw_migration.reset_unsubmitted_migration(self.tenant.id)
        Tenant.objects.filter(pk=self.tenant.pk).update(openclaw_version="2026.9.4")
        self.claim(status="FAILED")
        with self.assertRaisesMessage(MigrationError, "unexpected_target_revision"):
            openclaw_migration.reset_unsubmitted_migration(self.tenant.id)


@override_settings(**ON, TENANT_IDLE_HIBERNATE_MINUTES=30)
class TaskFlowTests(TestCase):
    """Drive the task with migrate_tenant replaced by scripted outcomes."""

    def setUp(self):
        self.tenant = tenant_fixture(94040)
        Tenant.objects.filter(pk=self.tenant.pk).update(last_message_at=timezone.now() - timedelta(hours=3))
        self.token = au.acquire(self.tenant)
        OpenClawAutoUpgrade.objects.create(tenant=self.tenant, run_token=self.token)
        self.publish = self.enterContext(patch("apps.cron.publish.publish_task"))
        self.hibernate = self.enterContext(
            patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
        )
        self.enterContext(patch("apps.orchestrator.hibernation._cron_active_or_imminent", return_value=None))
        self.rollback = self.enterContext(patch.object(openclaw_migration, "rollback_tenant", side_effect=self._rb))

    def _rb(self, tenant_id):
        Tenant.objects.filter(pk=tenant_id).update(
            openclaw_migration={"status": "ROLLED_BACK"}, openclaw_migration_cron_fenced=False
        )
        return {"status": "ROLLED_BACK"}

    def migrate(self, *, result=None, fail=None, undo=True):
        """migrate_tenant stand-in: claims with the caller's token like the real tool."""

        def run(tenant_id, tag, *, owner_token=None, **kwargs):
            self.assertEqual(tag, TAG)
            if fail is None:
                Tenant.objects.filter(pk=tenant_id).update(
                    openclaw_migration={"owner_token": owner_token, "status": "PASS"}, openclaw_version="2026.9.4"
                )
                return result or {"status": "PASS", "steps": list(openclaw_migration.STEPS)}
            status, step, reason = fail
            record = {"owner_token": owner_token, "status": status, "failure": {"step": step, "reason": reason}}
            if undo:
                record["undo"] = {"share_snapshot": "s"}
            Tenant.objects.filter(pk=tenant_id).update(openclaw_migration=record, openclaw_migration_cron_fenced=True)
            raise MigrationFailed("FAILED", status=status, step=step, reason=reason)

        return patch.object(openclaw_migration, "migrate_tenant", side_effect=run)

    def current_token(self):
        return OpenClawAutoUpgradeLock.objects.get(pk=1).run_token

    def run_task(self, phase="start"):
        return au.run_phase(str(self.tenant.id), self.current_token(), phase)

    def state(self):
        return OpenClawAutoUpgrade.objects.get(tenant=self.tenant)

    def assert_released(self):
        row = OpenClawAutoUpgradeLock.objects.get(pk=1)
        self.assertEqual(row.run_token, "")
        self.assertGreater(row.next_allowed_at, timezone.now() + timedelta(minutes=11))

    def test_tag_is_pinned_for_the_whole_run(self):
        state = self.state()
        state.counters = {"tag": TAG}
        state.save()
        with self.migrate(), override_settings(OPENCLAW_AUTO_UPGRADE_TAG="2026.9.4-bbbbbbb"):
            self.assertEqual(self.run_task("resume")["status"], "PASS")

    def test_pass_hibernates_and_releases(self):
        with self.migrate():
            result = self.run_task()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["hibernated"])
        self.hibernate.assert_called_once()
        self.assert_released()
        self.assertEqual(len(mail.outbox), 0)

    def test_stale_token_is_a_noop(self):
        with self.migrate() as migrate:
            self.assertEqual(au.run_phase(str(self.tenant.id), "stale", "start"), {"status": "stale_run"})
        migrate.assert_not_called()

    def test_config_failure_rolls_back_emails_and_cools_down(self):
        with self.migrate(fail=("FAILED", "config", "Operator command timed out or returned no result")):
            result = self.run_task()
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.rollback.assert_called_once_with(self.tenant.id)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.openclaw_migration_cron_fenced)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn(str(self.tenant.id)[:8], body)
        self.assertIn("step config", body)
        self.assertIn("migrate_tenant_openclaw --tenant", body)
        self.assertGreater(self.state().cooldown_until, timezone.now() + timedelta(days=6))
        self.hibernate.assert_called_once()
        self.assert_released()

    def test_preflight_failure_without_undo_resets_emails_and_cools_down(self):
        with self.migrate(fail=("FAILED", "preflight", "acr_digest_unavailable"), undo=False):
            result = self.run_task()
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.rollback.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.openclaw_migration_cron_fenced)
        self.assertEqual(self.tenant.openclaw_migration["status"], "ROLLED_BACK")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNotNone(self.state().cooldown_until)

    def test_verify_console_timeout_waits_then_resumes_twice_then_rolls_back(self):
        fail = ("FAILED", "verify", "Operator console failed (output withheld)")
        with self.migrate(fail=fail):
            first = self.run_task()
            self.assertEqual(first, {"status": "WAITING", "action": au.WAIT_RESUME, "delay_seconds": 720})
            self.publish.assert_called_with(
                au.TASK_NAME, str(self.tenant.id), self.current_token(), "resume", delay_seconds=720, retries=0
            )
            self.rollback.assert_not_called()
            self.assertTrue(au.holds(self.tenant.id, self.current_token()))
            self.assertNotEqual(self.current_token(), self.token)
            self.assertEqual(self.run_task("resume")["status"], "WAITING")
            final = self.run_task("resume")
        self.assertEqual(final["status"], "ROLLED_BACK")
        self.rollback.assert_called_once()
        self.assertEqual(len(mail.outbox), 1)

    def test_transient_then_pass(self):
        with self.migrate(fail=("FAILED", "crons", "Operator command timed out or returned no result")):
            self.run_task()
        with self.migrate():
            result = self.run_task("resume")
        self.assertEqual(result["status"], "PASS")
        self.rollback.assert_not_called()

    def test_defer_unavailable_rewakes_once_then_gives_up_quietly(self):
        defer = {"status": "DEFER", "reasons": {"tenant_unavailable": 1}, "steps": []}
        with self.migrate(result=defer):
            first = self.run_task()
            self.assertEqual(first["delay_seconds"], 300)
            second = self.run_task("resume")
        self.assertEqual(second["status"], "GAVE_UP")
        self.assertEqual(len(mail.outbox), 0)
        self.rollback.assert_not_called()
        self.assertLess(self.state().cooldown_until, timezone.now() + timedelta(hours=2))
        self.assert_released()

    def test_cron_imminent_gives_up_quietly(self):
        with self.migrate(result={"status": "DEFER", "reasons": {"cron_imminent": 1}, "steps": []}):
            result = self.run_task()
        self.assertEqual(result["status"], "GAVE_UP")
        self.assertEqual(len(mail.outbox), 0)

    def test_deferred_mid_image_resets_unfenced(self):
        with self.migrate(fail=("DEFERRED", "image", "cron_imminent"), undo=False):
            result = self.run_task()
        self.assertEqual(result["status"], "GAVE_UP")
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.openclaw_migration_cron_fenced)

    def test_blocked_unsupported_emails_and_cools_down(self):
        blocked = {"status": "BLOCKED_UNSUPPORTED", "reasons": {"agent_turn_unmapped": 2}, "steps": []}
        with self.migrate(result=blocked):
            result = self.run_task()
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("agent_turn_unmapped", mail.outbox[0].body)
        self.assertGreater(self.state().cooldown_until, timezone.now() + timedelta(days=6))

    def test_someone_elses_record_is_never_undone(self):
        def run(*args, **kwargs):
            raise MigrationFailed("FAILED", status="FAILED", step="crons", reason="migration_owner_fenced")

        Tenant.objects.filter(pk=self.tenant.pk).update(
            openclaw_migration={"owner_token": "operator", "status": "RUNNING"}, openclaw_migration_cron_fenced=True
        )
        with patch.object(openclaw_migration, "migrate_tenant", side_effect=run):
            self.run_task("resume")
        self.rollback.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_migration["owner_token"], "operator")
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(OPENCLAW_AUTO_UPGRADE_JEV_ENABLED=True)
    def test_unknown_failure_goes_to_jev_and_is_logged(self):
        verdict = {
            "choice": "transient_retry_later",
            "probabilities": {"transient_retry_later": 0.95, "needs_rollback_now": 0.03, "needs_human": 0.02},
            "confidence": 0.95,
        }
        with (
            self.migrate(fail=("FAILED", "verify", "canonical_content_mismatch")),
            patch.object(au, "classify_with_jev", return_value=verdict),
        ):
            first = self.run_task()
            self.assertEqual(first["action"], au.WAIT_RESUME)
            self.tenant.refresh_from_db()
            self.assertEqual(
                self.tenant.openclaw_migration["auto_upgrade_decisions"][0]["verdict"]["choice"],
                "transient_retry_later",
            )
            final = self.run_task("resume")
        self.assertEqual(final["status"], "ROLLED_BACK")
        self.assertEqual(self.state().counters["jev_resumes"], 1)

    def test_unknown_failure_without_jev_rolls_back(self):
        with self.migrate(fail=("FAILED", "verify", "canonical_content_mismatch")):
            result = self.run_task()
        self.assertEqual(result["status"], "ROLLED_BACK")

    def test_failed_resume_schedule_exits_safely(self):
        self.publish.side_effect = RuntimeError("qstash down")
        with self.migrate(fail=("FAILED", "crons", "Operator command timed out or returned no result")):
            result = self.run_task()
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.openclaw_migration_cron_fenced)

    def test_hibernated_tenant_is_woken_then_settles(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(hibernated_at=timezone.now())
        with (
            patch("apps.orchestrator.hibernation.wake_hibernated_tenant", return_value=True) as wake,
            self.migrate() as migrate,
        ):
            result = self.run_task()
        wake.assert_called_once()
        self.assertTrue(wake.call_args.kwargs["cron_wake"])
        migrate.assert_not_called()
        self.assertEqual(result["delay_seconds"], au.WAKE_SETTLE_SECONDS)

    def test_awake_tenant_gets_the_cron_shield(self):
        with self.migrate():
            self.run_task()
        # Stamped before the run (then cleared by the real hibernate, mocked here).
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.cron_wake_at)

    def test_user_returned_during_run_is_not_hibernated(self):
        def run(tenant_id, tag, *, owner_token=None, **kwargs):
            Tenant.objects.filter(pk=tenant_id).update(
                openclaw_migration={"owner_token": owner_token, "status": "PASS"}, last_message_at=timezone.now()
            )
            return {"status": "PASS", "steps": []}

        with patch.object(openclaw_migration, "migrate_tenant", side_effect=run):
            result = self.run_task()
        self.assertFalse(result["hibernated"])
        self.hibernate.assert_not_called()

    def test_unexpected_error_still_exits_safely(self):
        with self.migrate(fail=("FAILED", "config", "x")), patch.object(au, "decide", side_effect=KeyError("bug")):
            result = self.run_task()
        self.assertEqual(result["status"], "STOPPED")
        self.rollback.assert_called_once()
        self.assertEqual(len(mail.outbox), 1)
        self.assert_released()

    # -- recovery after the worker died -------------------------------------

    def test_recover_takes_over_an_expired_running_record_once(self):
        state = self.state()
        state.migration_token = "dead"
        state.save()
        Tenant.objects.filter(pk=self.tenant.pk).update(
            openclaw_migration={
                "owner_token": "dead",
                "status": "RUNNING",
                "lease_until": (timezone.now() - timedelta(minutes=1)).isoformat(),
            },
            openclaw_migration_cron_fenced=True,
        )
        with self.migrate() as migrate:
            result = self.run_task("recover")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(migrate.call_args.kwargs["takeover"], "dead")
        self.assertTrue(migrate.call_args.kwargs["confirm_owner_dead"])

    def test_recover_waits_for_a_live_record_lease(self):
        state = self.state()
        state.migration_token = "maybe-alive"
        state.save()
        Tenant.objects.filter(pk=self.tenant.pk).update(
            openclaw_migration={
                "owner_token": "maybe-alive",
                "status": "RUNNING",
                "lease_until": (timezone.now() + timedelta(minutes=4)).isoformat(),
            }
        )
        with self.migrate() as migrate:
            result = self.run_task("recover")
        migrate.assert_not_called()
        self.assertEqual(result["action"], "await_record_lease")

    def test_recover_after_takeover_budget_rolls_back(self):
        state = self.state()
        state.migration_token = "dead"
        state.counters = {"takeovers": 1}
        state.save()
        Tenant.objects.filter(pk=self.tenant.pk).update(
            openclaw_migration={
                "owner_token": "dead",
                "status": "RUNNING",
                "undo": {"share_snapshot": "s"},
                "lease_until": (timezone.now() - timedelta(minutes=1)).isoformat(),
            },
            openclaw_migration_cron_fenced=True,
        )
        result = self.run_task("recover")
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.rollback.assert_called_once()
        self.assertEqual(len(mail.outbox), 1)

    def test_recover_with_nothing_claimed_is_a_noop(self):
        with self.migrate() as migrate:
            result = self.run_task("recover")
        migrate.assert_not_called()
        self.assertEqual(result["status"], "RECOVERED_NOOP")
        self.assert_released()


class DeliveryTests(TestCase):
    def setUp(self):
        self.tenant = tenant_fixture(94060)
        self.token = au.acquire(self.tenant)

    def test_each_delivery_is_single_use(self):
        fresh = au.claim(self.tenant.id, self.token)
        self.assertTrue(fresh)
        self.assertIsNone(au.claim(self.tenant.id, self.token))
        self.assertTrue(au.holds(self.tenant.id, fresh))

    def test_task_spawns_a_detached_phase_process(self):
        with patch("subprocess.Popen") as popen:
            result = au.auto_upgrade_openclaw_task(str(self.tenant.id), self.token, "resume")
        self.assertEqual(result, {"status": "spawned", "phase": "resume"})
        argv = popen.call_args.args[0]
        self.assertEqual(argv[2:], ["auto_upgrade_openclaw", str(self.tenant.id), self.token, "resume"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_task_with_stale_token_spawns_nothing(self):
        with patch("subprocess.Popen") as popen:
            self.assertEqual(au.auto_upgrade_openclaw_task(str(self.tenant.id), "0" * 32), {"status": "stale_run"})
        popen.assert_not_called()

    def test_registered_with_the_trigger_endpoint(self):
        from apps.cron.views import TASK_MAP

        self.assertEqual(TASK_MAP[au.TASK_NAME], "apps.orchestrator.openclaw_auto_upgrade.auto_upgrade_openclaw_task")

    @override_settings(**ON)
    def test_command_runs_the_phase(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with (
            patch.object(openclaw_migration, "migrate_tenant", side_effect=MigrationError("Existing migration")),
            patch.object(au, "_hibernate_if_idle", return_value=False),
        ):
            call_command("auto_upgrade_openclaw", str(self.tenant.id), self.token, "resume", stdout=out)
        self.assertIn("status=STOPPED", out.getvalue())


class MigrationFailedContractTests(TestCase):
    """migrate_tenant exposes the checkpointed outcome, CLI text unchanged."""

    def test_failed_run_raises_structured_outcome(self):
        tenant = tenant_fixture(94050)
        with (
            patch.object(openclaw_migration, "report_tenant", return_value={"status": "READY", "reasons": {}}),
            patch.dict(
                openclaw_migration.HANDLERS,
                {"preflight": lambda t, r: (_ for _ in ()).throw(MigrationError("acr_digest_unavailable"))},
            ),
            self.assertRaises(MigrationFailed) as ctx,
        ):
            openclaw_migration.migrate_tenant(tenant.id, TAG, owner_token="given")
        exc = ctx.exception
        self.assertEqual((exc.status, exc.step, exc.reason), ("FAILED", "preflight", "acr_digest_unavailable"))
        self.assertTrue(str(exc).startswith("FAILED at preflight: acr_digest_unavailable."))
        tenant.refresh_from_db()
        self.assertEqual(tenant.openclaw_migration["owner_token"], "given")
