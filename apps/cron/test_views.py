"""Tests for QStash cron trigger endpoints."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import call, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.cron.models import CronJob, CronJobSource
from apps.cron.share_observer import ShareObservation
from apps.crypto import audit, box
from apps.crypto.keys import mint_and_wrap_dek
from apps.router import enc_columns
from apps.router.models import ChatThread, DeviceToken
from apps.tenants.models import Tenant, User

_RAW_PIN_TITLE = "📍 Current location: 34.69337, 135.49415 (±12m)"
_SAFE_PIN_TITLE = "📍 Current location: … (±12m)"


def _create_tenant_with_config_state(
    *,
    active: bool = True,
    config_version: int = 0,
    pending_config_version: int = 0,
    last_message_at=None,
    has_container: bool = True,
    suffix: int = 0,
):
    user = User.objects.create_user(
        username=f"user-{pending_config_version}-{config_version}-{suffix}", password="testpass123"
    )
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE if active else Tenant.Status.PENDING,
        model_tier=Tenant.ModelTier.STARTER,
        container_id="oc-test" if has_container else "",
        container_fqdn="oc-test.internal.azurecontainerapps.io" if has_container else "",
        config_version=config_version,
        pending_config_version=pending_config_version,
        last_message_at=last_message_at,
    )
    return tenant


class ApplyPendingConfigsTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=lambda tasks: len(tasks))
    def test_apply_pending_configs_enqueues_idle_tenants_only(self, mock_batch, mock_verify):
        now = timezone.now()
        ready = _create_tenant_with_config_state(
            pending_config_version=2,
            config_version=1,
            last_message_at=None,
            suffix=1,
        )
        stale = _create_tenant_with_config_state(
            pending_config_version=2,
            config_version=1,
            last_message_at=now - timedelta(minutes=16),
            has_container=True,
            suffix=2,
        )
        active_recent = _create_tenant_with_config_state(
            pending_config_version=2,
            config_version=1,
            last_message_at=now - timedelta(minutes=5),
            suffix=3,
        )
        updated_pending = _create_tenant_with_config_state(
            pending_config_version=1,
            config_version=1,
            last_message_at=now - timedelta(minutes=40),
            suffix=4,
        )
        inactive = _create_tenant_with_config_state(
            active=False,
            pending_config_version=2,
            config_version=0,
            last_message_at=None,
            suffix=5,
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["config_enqueued"], 2)
        self.assertEqual(body["config_failed"], 0)
        self.assertEqual(body["evaluated"], 2)

        # Verify publish_batch was called with tasks for each eligible tenant
        batch_tasks = mock_batch.call_args[0][0]
        config_calls = [t for t in batch_tasks if t[0] == "apply_single_tenant_config"]
        self.assertEqual(len(config_calls), 2)

        # Config version NOT bumped yet — that happens in the async task
        ready.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(ready.config_version, 1)
        self.assertEqual(stale.config_version, 1)


@override_settings(DEPLOY_SECRET="test-deploy-secret")
class BumpAllPendingConfigsTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _post(self):
        return self.client.post(
            "/api/v1/cron/bump-all-pending-configs/",
            HTTP_X_DEPLOY_SECRET="test-deploy-secret",
        )

    def _age_past_channel_grace_period(self, *tenants):
        Tenant.objects.filter(pk__in=[tenant.pk for tenant in tenants]).update(
            created_at=timezone.now() - timedelta(days=2)
        )

    def test_app_only_tenant_is_bumped(self):
        tenant = _create_tenant_with_config_state(
            config_version=4,
            pending_config_version=4,
            suffix=10,
        )
        self._age_past_channel_grace_period(tenant)
        DeviceToken.objects.create(
            user=tenant.user,
            tenant=tenant,
            token="a" * 64,
            environment=DeviceToken.Environment.SANDBOX,
        )

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queued"], 1)
        tenant.refresh_from_db()
        self.assertEqual(tenant.config_version, 4)
        self.assertEqual(tenant.pending_config_version, 5)

    def test_tenant_without_any_channel_is_not_bumped(self):
        tenant = _create_tenant_with_config_state(
            config_version=4,
            pending_config_version=4,
            suffix=11,
        )
        self._age_past_channel_grace_period(tenant)
        self.assertFalse(tenant.device_tokens.exists())

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queued"], 0)
        tenant.refresh_from_db()
        self.assertEqual(tenant.config_version, 0)
        self.assertEqual(tenant.pending_config_version, 0)

    def test_telegram_and_line_tenants_are_still_bumped(self):
        telegram_tenant = _create_tenant_with_config_state(
            config_version=2,
            pending_config_version=2,
            suffix=12,
        )
        line_tenant = _create_tenant_with_config_state(
            config_version=7,
            pending_config_version=7,
            suffix=13,
        )
        User.objects.filter(pk=telegram_tenant.user_id).update(telegram_chat_id=123456)
        User.objects.filter(pk=line_tenant.user_id).update(line_user_id="U-test-channel")
        self._age_past_channel_grace_period(telegram_tenant, line_tenant)

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queued"], 2)
        telegram_tenant.refresh_from_db()
        line_tenant.refresh_from_db()
        self.assertEqual(telegram_tenant.pending_config_version, 3)
        self.assertEqual(line_tenant.pending_config_version, 8)


@override_settings(DEPLOY_SECRET="test-deploy-secret")
class BackfillWelcomesTransitionRegressionTest(TestCase):
    """An unstamped veteran is scheduled once, then the schedule stamp closes the loop."""

    def setUp(self):
        self.client = APIClient()
        self.tenant = _create_tenant_with_config_state(suffix=20)
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])

    def _post(self):
        return self.client.post(
            "/api/v1/cron/backfill-welcomes/",
            HTTP_X_DEPLOY_SECRET="test-deploy-secret",
        )

    @patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={})
    @patch("apps.cron.gateway_client.cron_get", return_value=None)
    def test_grandfathered_veteran_without_cron_schedules_once_and_stamps(
        self,
        mock_cron_get,
        mock_invoke,
    ):
        first = self._post()
        second = self._post()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["fuel"], {"scheduled": 1})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["fuel"], {"skipped_already_delivered": 1})

        self.tenant.refresh_from_db()
        self.assertIn("fuel", self.tenant.welcomes_sent)
        mock_cron_get.assert_called_once()
        mock_invoke.assert_called_once()
        self.assertEqual(mock_invoke.call_args.args[1], "cron.add")

    def test_first_session_stamp_is_not_a_backfill_feature(self):
        self.tenant.fuel_enabled = False
        self.tenant.welcomes_sent = {"first_session": "2026-08-02T00:00:00+00:00"}
        self.tenant.save(update_fields=["fuel_enabled", "welcomes_sent"])

        with patch(
            "apps.orchestrator.first_session_welcome.seed_first_session_welcome",
            side_effect=AssertionError("fleet backfill must not seed first-session welcomes"),
        ) as first_session:
            response = self._post()

        self.assertEqual(response.status_code, 200)
        first_session.assert_not_called()
        self.assertEqual(set(response.json()), {"tenants_walked", "fuel", "finance", "statuses"})
        self.assertEqual(response.json()["fuel"], {})
        self.assertEqual(response.json()["finance"], {})


class CronAuthTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_apply_pending_configs_rejects_invalid_signature(self):
        response = self.client.post("/api/v1/cron/apply-pending-configs/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid signature")


@override_settings(DEPLOY_SECRET="test-deploy-secret")
class ScrubThreadTitlesCronTest(TestCase):
    def setUp(self):
        patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = APIClient()
        self.tenant = self._create_tenant("target")

    def _create_tenant(self, suffix: str) -> Tenant:
        user = User.objects.create_user(
            username=f"title-scrub-cron-{suffix}",
            email=f"title-scrub-cron-{suffix}@example.com",
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            encrypt_chat_writes=True,
            read_encrypted_chat=True,
        )
        mint_and_wrap_dek(tenant)
        return tenant

    def _create_thread(self, tenant: Tenant) -> ChatThread:
        return ChatThread.objects.create(
            tenant=tenant,
            user=tenant.user,
            title=_RAW_PIN_TITLE,
            title_enc=box.encrypt(
                tenant.id,
                *enc_columns.CHAT_THREAD_TITLE,
                _RAW_PIN_TITLE,
            ),
        )

    def _post(self, query: str = ""):
        return self.client.post(
            f"/api/cron/scrub-thread-titles/{query}",
            HTTP_X_DEPLOY_SECRET="test-deploy-secret",
        )

    def _reveal_title(self, thread: ChatThread) -> str:
        return box.decrypt(
            thread.tenant_id,
            *enc_columns.CHAT_THREAD_TITLE,
            bytes(thread.title_enc),
        ).reveal()

    def test_auth_required(self):
        response = self.client.post("/api/cron/scrub-thread-titles/")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Unauthorized")

    def test_default_is_dry_run_and_changes_no_rows(self):
        thread = self._create_thread(self.tenant)

        response = self._post(f"?tenant_id={self.tenant.id}")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            {key: response.json()[key] for key in ("dry_run", "scanned", "changed", "errors", "tenants")},
            {"dry_run": True, "scanned": 1, "changed": 1, "errors": 0, "tenants": 1},
        )
        thread.refresh_from_db()
        self.assertEqual(thread.title, _RAW_PIN_TITLE)
        self.assertEqual(self._reveal_title(thread), _RAW_PIN_TITLE)

    def test_apply_one_changes_rows(self):
        thread = self._create_thread(self.tenant)

        response = self._post(f"?apply=1&tenant_id={self.tenant.id}")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            {key: response.json()[key] for key in ("dry_run", "scanned", "changed", "errors", "tenants")},
            {"dry_run": False, "scanned": 1, "changed": 1, "errors": 0, "tenants": 1},
        )
        thread.refresh_from_db()
        self.assertEqual(thread.title, _SAFE_PIN_TITLE)
        self.assertEqual(self._reveal_title(thread), _SAFE_PIN_TITLE)

    def test_tenant_scoping_is_honored(self):
        target_thread = self._create_thread(self.tenant)
        other_tenant = self._create_tenant("other")
        other_thread = self._create_thread(other_tenant)

        response = self._post(f"?apply=1&tenant_id={self.tenant.id}")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["tenant_id"], str(self.tenant.id))
        self.assertEqual(response.json()["scanned"], 1)
        self.assertEqual(response.json()["changed"], 1)
        target_thread.refresh_from_db()
        other_thread.refresh_from_db()
        self.assertEqual(target_thread.title, _SAFE_PIN_TITLE)
        self.assertEqual(other_thread.title, _RAW_PIN_TITLE)
        self.assertEqual(self._reveal_title(other_thread), _RAW_PIN_TITLE)


@override_settings(DEPLOY_SECRET="test-deploy-secret")
class CronOpsTriggerTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(
            username="cron-ops-trigger",
            email="cron-ops-trigger@example.com",
        )
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_id="oc-cron-ops-trigger",
            container_fqdn="oc-cron-ops-trigger.internal",
            postgres_cron_canonical=True,
        )

    def _post(self, path: str, body: dict, *, authenticated: bool = True):
        headers = {"HTTP_X_DEPLOY_SECRET": "test-deploy-secret"} if authenticated else {}
        return self.client.post(
            path,
            data=json.dumps(body),
            content_type="application/json",
            **headers,
        )

    def _observation(self, jobs: list[dict]) -> ShareObservation:
        now = timezone.now()
        return ShareObservation(
            tenant_id=str(self.tenant.id),
            jobs=tuple(jobs),
            jobs_state={job["id"]: {} for job in jobs},
            count=len(jobs),
            digest="a" * 64,
            mtime=now,
            observed_at=now,
        )

    def test_auth_required_for_all_ops_triggers(self):
        requests = (
            (
                "/api/cron/repair-fuel-rows/",
                {"tenant_id": str(self.tenant.id), "confirm": False},
            ),
            (
                "/api/cron/retire-quarantined/",
                {
                    "tenant_id": str(self.tenant.id),
                    "name": "_fuel:welcome",
                    "bucket": "duplicate",
                    "limit": 1,
                    "confirm": False,
                },
            ),
            (
                "/api/cron/delete-registry-cron/",
                {"tenant_id": str(self.tenant.id), "name": "Reminder"},
            ),
        )

        for path, body in requests:
            with self.subTest(path=path):
                response = self._post(path, body, authenticated=False)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"error": "Unauthorized"})

    def test_unknown_fields_are_rejected_for_all_ops_triggers(self):
        requests = (
            (
                "/api/cron/repair-fuel-rows/",
                {"tenant_id": str(self.tenant.id), "confirm": False},
            ),
            (
                "/api/cron/retire-quarantined/",
                {
                    "tenant_id": str(self.tenant.id),
                    "name": "_fuel:welcome",
                    "bucket": "duplicate",
                    "limit": 1,
                    "confirm": False,
                },
            ),
            (
                "/api/cron/delete-registry-cron/",
                {"tenant_id": str(self.tenant.id), "name": "Reminder"},
            ),
        )

        for path, body in requests:
            with self.subTest(path=path):
                response = self._post(path, {**body, "surprise": True})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json(),
                    {"error": "Unknown fields", "fields": ["surprise"]},
                )

    def test_repair_fuel_rows_dry_run_reports_names_without_writing(self):
        row = CronJob.objects.create(
            tenant=self.tenant,
            name="_fuel:welcome",
            data={},
            source=CronJobSource.SYSTEM,
            managed=True,
            enabled=True,
        )

        response = self._post(
            "/api/cron/repair-fuel-rows/",
            {"tenant_id": str(self.tenant.id), "confirm": False},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            {key: response.json()[key] for key in ("matched", "retired", "already_retired")},
            {"matched": 1, "retired": 0, "already_retired": 0},
        )
        self.assertEqual([item["name"] for item in response.json()["rows"]], ["_fuel:welcome"])
        row.refresh_from_db()
        self.assertTrue(row.enabled)
        self.assertTrue(row.managed)

    def test_repair_fuel_rows_confirm_retires_matches(self):
        row = CronJob.objects.create(
            tenant=self.tenant,
            name="_sync:stale",
            data={},
            source=CronJobSource.AGENT,
            managed=True,
            enabled=True,
        )

        response = self._post(
            "/api/cron/repair-fuel-rows/",
            {"tenant_id": str(self.tenant.id), "confirm": True},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["retired"], 1)
        row.refresh_from_db()
        self.assertFalse(row.enabled)
        self.assertFalse(row.managed)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    @patch("apps.cron.share_observer.observe_share")
    def test_retire_quarantined_dry_run_lists_without_removing(self, mock_observe, mock_gateway):
        mock_observe.return_value = self._observation(
            [
                {"id": "job-1", "name": "_fuel:welcome"},
                {"id": "other", "name": "Other"},
            ]
        )

        response = self._post(
            "/api/cron/retire-quarantined/",
            {
                "tenant_id": str(self.tenant.id),
                "name": "_fuel:welcome",
                "bucket": "duplicate",
                "limit": 10,
                "confirm": False,
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            {key: response.json()[key] for key in ("matched", "removed", "failed", "remaining", "removed_ids")},
            {
                "matched": 1,
                "removed": 0,
                "failed": 0,
                "remaining": 1,
                "removed_ids": [],
            },
        )
        mock_gateway.assert_not_called()

    @patch("apps.cron.management.commands.retire_quarantined.time.sleep")
    @patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={})
    @patch("apps.cron.share_observer.observe_share")
    def test_retire_quarantined_confirm_removes_exact_ids(
        self,
        mock_observe,
        mock_gateway,
        mock_sleep,
    ):
        mock_observe.return_value = self._observation(
            [
                {"id": "job-1", "name": "_fuel:welcome"},
                {"id": "job-2", "name": "_fuel:welcome"},
            ]
        )

        response = self._post(
            "/api/cron/retire-quarantined/",
            {
                "tenant_id": str(self.tenant.id),
                "name": "_fuel:welcome",
                "bucket": "expired",
                "limit": 2,
                "confirm": True,
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["removed_ids"], ["job-1", "job-2"])
        self.assertEqual(
            mock_gateway.call_args_list,
            [
                call(self.tenant, "cron.remove", {"jobId": "job-1"}),
                call(self.tenant, "cron.remove", {"jobId": "job-2"}),
            ],
        )
        mock_sleep.assert_called_once()

    @patch("apps.cron.share_observer.observe_share")
    def test_retire_quarantined_hard_cap_is_enforced_before_observation(self, mock_observe):
        response = self._post(
            "/api/cron/retire-quarantined/",
            {
                "tenant_id": str(self.tenant.id),
                "name": "_fuel:welcome",
                "bucket": "duplicate",
                "limit": 101,
                "confirm": True,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "--limit must be between 1 and 100")
        mock_observe.assert_not_called()

    @patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={})
    def test_delete_registry_cron_deletes_managed_row_and_bound_gateway_id(self, mock_gateway):
        row = CronJob.objects.create(
            tenant=self.tenant,
            name="Zombie Reminder",
            gateway_job_id="gateway-job-1",
            data={},
            source=CronJobSource.USER,
            managed=True,
            enabled=True,
        )

        response = self._post(
            "/api/cron/delete-registry-cron/",
            {"tenant_id": str(self.tenant.id), "name": row.name},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["deleted"],
            {
                "id": str(row.id),
                "name": "Zombie Reminder",
                "gateway_job_id": "gateway-job-1",
            },
        )
        self.assertTrue(response.json()["gateway_removal_succeeded"])
        self.assertFalse(CronJob.objects.filter(pk=row.pk).exists())
        mock_gateway.assert_called_once_with(
            self.tenant,
            "cron.remove",
            {"jobId": "gateway-job-1"},
        )

    def test_delete_registry_cron_returns_404_when_no_managed_row_matches(self):
        CronJob.objects.create(
            tenant=self.tenant,
            name="Unmanaged",
            data={},
            source=CronJobSource.AGENT,
            managed=False,
            enabled=True,
        )

        response = self._post(
            "/api/cron/delete-registry-cron/",
            {"tenant_id": str(self.tenant.id), "name": "Unmanaged"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Cron job not found"})
        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name="Unmanaged").exists())


class ExpireTrialsCronTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(username="trial-expired-owner", password="testpass123")
        self.user = user

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_expire_trials_suspends_unpaid_expired_trials(self, mock_verify):
        expired_trial = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            is_trial=True,
            trial_started_at=timezone.now() - timedelta(days=8),
            trial_ends_at=timezone.now() - timedelta(hours=1),
            stripe_subscription_id="",
        )

        active_premium = Tenant.objects.create(
            user=User.objects.create_user(username="trial-paid-owner", password="testpass123"),
            status=Tenant.Status.ACTIVE,
            is_trial=True,
            trial_started_at=timezone.now() - timedelta(days=8),
            trial_ends_at=timezone.now() - timedelta(hours=1),
            stripe_subscription_id="sub_123",
        )

        response = self.client.post("/api/v1/cron/expire-trials/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], 1)

        expired_trial.refresh_from_db()
        active_premium.refresh_from_db()

        self.assertFalse(expired_trial.is_trial)
        self.assertEqual(expired_trial.status, Tenant.Status.SUSPENDED)
        self.assertEqual(active_premium.status, Tenant.Status.ACTIVE)
        self.assertTrue(active_premium.is_trial)
        self.assertEqual(active_premium.stripe_subscription_id, "sub_123")

    @patch("apps.cron.views.verify_qstash_signature", return_value=False)
    def test_expire_trials_rejects_invalid_or_missing_signature(self, mock_verify):
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid signature")


class RestartTenantContainerTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.views.restart_container_app")
    def test_restart_tenant_container_calls_restart(self, mock_restart, mock_verify):
        user = User.objects.create_user(username="tenant-restart", password="testpass123")
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            model_tier=Tenant.ModelTier.STARTER,
            container_id="oc-restart-test",
            container_fqdn="oc-restart.internal.azurecontainerapps.io",
        )

        response = self.client.post(
            "/api/v1/cron/restart-tenant-container/",
            data=json.dumps({"tenant_id": str(tenant.id)}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["restarted"], True)
        self.assertEqual(response.json()["container"], "oc-restart-test")
        mock_restart.assert_called_once_with("oc-restart-test")

    @patch("apps.cron.views.verify_qstash_signature", return_value=False)
    def test_restart_tenant_container_rejects_invalid_signature(self, mock_verify):
        response = self.client.post("/api/v1/cron/restart-tenant-container/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid signature")


class ExpireTrialsEntitlementTest(TestCase):
    """Regression guards for the broadened entitlement query.

    The bug this prevents: production had 17 tenants with
    ``is_trial=False, status='active', no Stripe sub`` and trial_ends_at
    in the past. The earlier query filtered on ``is_trial=True`` so
    these ghost tenants were silently skipped on every daily sweep,
    accumulating LLM cost. The new query matches by ENTITLEMENT, not
    the ``is_trial`` flag.
    """

    def setUp(self):
        self.client = APIClient()

    def _make_tenant(self, *, suffix: str, **kwargs) -> Tenant:
        user = User.objects.create_user(username=f"ent-{suffix}", password="x")
        defaults = dict(
            user=user,
            status=Tenant.Status.ACTIVE,
            model_tier=Tenant.ModelTier.STARTER,
            container_id=f"oc-{suffix}",
            container_fqdn=f"oc-{suffix}.internal.azurecontainerapps.io",
        )
        defaults.update(kwargs)
        return Tenant.objects.create(**defaults)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 3, "errors": 0})
    def test_matches_ghost_state_unentitled_active(self, mock_suspend, mock_hibernate, mock_verify):
        """Tenant with is_trial=False, active, no sub, trial_ended is matched."""
        ghost = self._make_tenant(
            suffix="ghost",
            is_trial=False,
            trial_ends_at=timezone.now() - timedelta(days=20),
            stripe_subscription_id="",
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["updated"], 1)

        ghost.refresh_from_db()
        self.assertEqual(ghost.status, Tenant.Status.SUSPENDED)
        self.assertFalse(ghost.is_trial)
        mock_suspend.assert_called_once()
        mock_hibernate.assert_called_once_with(ghost.container_id)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 0, "errors": 0})
    def test_matches_classic_trial_expired(self, mock_suspend, mock_hibernate, mock_verify):
        """Original behavior preserved: is_trial=True with trial_ended is matched."""
        trial_user = self._make_tenant(
            suffix="trial",
            is_trial=True,
            trial_ends_at=timezone.now() - timedelta(days=1),
            stripe_subscription_id="",
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.json()["updated"], 1)

        trial_user.refresh_from_db()
        self.assertEqual(trial_user.status, Tenant.Status.SUSPENDED)
        self.assertFalse(trial_user.is_trial)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 0, "errors": 0})
    def test_skips_paid_tenant(self, mock_suspend, mock_hibernate, mock_verify):
        """Tenant with a Stripe subscription is skipped regardless of trial state."""
        paid = self._make_tenant(
            suffix="paid",
            is_trial=False,
            trial_ends_at=timezone.now() - timedelta(days=20),
            stripe_subscription_id="sub_real",
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.json()["updated"], 0)

        paid.refresh_from_db()
        self.assertEqual(paid.status, Tenant.Status.ACTIVE)
        mock_suspend.assert_not_called()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 0, "errors": 0})
    def test_skips_active_trial(self, mock_suspend, mock_hibernate, mock_verify):
        """Tenant on a valid (unexpired) trial is skipped."""
        active_trial = self._make_tenant(
            suffix="onTrial",
            is_trial=True,
            trial_ends_at=timezone.now() + timedelta(days=5),
            stripe_subscription_id="",
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        self.assertEqual(response.json()["updated"], 0)

        active_trial.refresh_from_db()
        self.assertEqual(active_trial.status, Tenant.Status.ACTIVE)
        self.assertTrue(active_trial.is_trial)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.orchestrator.azure_client.hibernate_container_app", return_value=None)
    @patch("apps.cron.suspension.suspend_tenant_crons", return_value={"disabled": 0, "errors": 0})
    def test_reports_already_hibernated_separately(self, mock_suspend, mock_hibernate, mock_verify):
        """Ghost tenants that are already hibernated are counted separately."""
        ghost = self._make_tenant(
            suffix="ghosthibe",
            is_trial=False,
            trial_ends_at=timezone.now() - timedelta(days=20),
            stripe_subscription_id="",
            hibernated_at=timezone.now() - timedelta(days=7),
        )
        response = self.client.post("/api/v1/cron/expire-trials/")
        body = response.json()
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["already_hibernated"], 1)

        ghost.refresh_from_db()
        self.assertEqual(ghost.status, Tenant.Status.SUSPENDED)


class TriggerTaskArgValidationTest(TestCase):
    """Boundary-hardening for ``trigger_task`` — issue #557.

    Pre-fix, every QStash delivery with a malformed/empty body would
    fall through to the underlying task with no positional args, raise
    ``TypeError`` inside the task, and return 500. QStash retries 5xx
    three times, so one bad message turned into three 500s + a DLQ
    park. The fix is to validate ``(args, kwargs)`` against the task
    signature at the boundary and return 400 instead — QStash does
    not retry 4xx, so the message is parked on the first delivery.

    See also: the QStash MCP ``qstash_publish_message`` tool sends bodies
    via the generic ``publish`` path which corrupts JSON for Django's
    receiver; multiple unrelated triggers hit this during the #540
    flip-cycle verification on 2026-05-18.
    """

    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_empty_body_against_no_arg_task_succeeds(self, mock_verify):
        """``reset_daily_counters_task`` takes no args; empty body should run."""
        with patch("apps.tenants.tasks.reset_daily_counters_task", autospec=True) as mock_task:
            mock_task.return_value = None
            response = self.client.post(
                "/api/v1/cron/trigger/reset_daily_counters/",
                data=b"",
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        mock_task.assert_called_once_with()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_empty_body_against_required_arg_task_returns_400(self, mock_verify):
        """Pre-fix this returned 500 → QStash retried 3x. Now 400."""
        with patch("apps.orchestrator.tasks.apply_single_tenant_config_task", autospec=True) as mock_task:
            response = self.client.post(
                "/api/v1/cron/trigger/apply_single_tenant_config/",
                data=b"",
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("missing", body["error"].lower())
        # Critical: the underlying task must NOT run when args are bad.
        mock_task.assert_not_called()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_empty_json_object_against_required_arg_task_returns_400(self, mock_verify):
        """``{}`` is well-formed JSON but lacks ``args`` → same as empty."""
        with patch("apps.orchestrator.tasks.apply_single_tenant_config_task", autospec=True) as mock_task:
            response = self.client.post(
                "/api/v1/cron/trigger/apply_single_tenant_config/",
                data=b"{}",
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        mock_task.assert_not_called()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_malformed_json_against_required_arg_task_returns_400(self, mock_verify):
        """Bad JSON → JSONDecodeError swallowed → empty args → 400."""
        with patch("apps.orchestrator.tasks.apply_single_tenant_config_task", autospec=True) as mock_task:
            response = self.client.post(
                "/api/v1/cron/trigger/apply_single_tenant_config/",
                data=b"not json at all",
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        mock_task.assert_not_called()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_double_encoded_string_body_returns_400(self, mock_verify):
        """The QStash MCP ``qstash_publish_message`` failure mode: body
        arrives as a JSON-encoded string ``"{\\"args\\":[\\"x\\"]}"`` instead
        of a JSON object. ``.get("args", [])`` on a string raises
        AttributeError → swallowed → empty args → 400 (not 500).
        """
        with patch("apps.orchestrator.tasks.apply_single_tenant_config_task", autospec=True) as mock_task:
            response = self.client.post(
                "/api/v1/cron/trigger/apply_single_tenant_config/",
                data=b'"some string"',
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        mock_task.assert_not_called()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_null_args_is_coerced_and_returns_400(self, mock_verify):
        """``{"args": null}`` previously crashed during ``*null`` unpacking
        with an opaque TypeError → 500. Now coerced to empty list → 400.
        """
        with patch("apps.orchestrator.tasks.apply_single_tenant_config_task", autospec=True) as mock_task:
            response = self.client.post(
                "/api/v1/cron/trigger/apply_single_tenant_config/",
                data=json.dumps({"args": None}).encode(),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        mock_task.assert_not_called()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_well_formed_body_with_correct_args_succeeds(self, mock_verify):
        """Regression guard — valid messages keep working."""
        with patch("apps.orchestrator.tasks.apply_single_tenant_config_task", autospec=True) as mock_task:
            mock_task.return_value = None
            response = self.client.post(
                "/api/v1/cron/trigger/apply_single_tenant_config/",
                data=json.dumps({"args": ["fake-tenant-uuid"], "kwargs": {}}).encode(),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        mock_task.assert_called_once_with("fake-tenant-uuid")

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_too_many_positional_args_returns_400(self, mock_verify):
        with patch("apps.orchestrator.tasks.apply_single_tenant_config_task", autospec=True) as mock_task:
            response = self.client.post(
                "/api/v1/cron/trigger/apply_single_tenant_config/",
                data=json.dumps({"args": ["uuid", "extra", "also-extra"], "kwargs": {}}).encode(),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("too many", body["error"].lower())
        mock_task.assert_not_called()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_unknown_kwarg_returns_400(self, mock_verify):
        with patch("apps.orchestrator.tasks.apply_single_tenant_config_task", autospec=True) as mock_task:
            response = self.client.post(
                "/api/v1/cron/trigger/apply_single_tenant_config/",
                data=json.dumps({"args": ["uuid"], "kwargs": {"bogus_kwarg": True}}).encode(),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("unexpected", body["error"].lower())
        mock_task.assert_not_called()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_known_kwarg_is_accepted(self, mock_verify):
        """``apply_single_tenant_config_task`` accepts ``_is_followup_retry`` kwarg."""
        with patch("apps.orchestrator.tasks.apply_single_tenant_config_task", autospec=True) as mock_task:
            mock_task.return_value = None
            response = self.client.post(
                "/api/v1/cron/trigger/apply_single_tenant_config/",
                data=json.dumps({"args": ["uuid"], "kwargs": {"_is_followup_retry": True}}).encode(),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        mock_task.assert_called_once_with("uuid", _is_followup_retry=True)


class TriggerTaskPrincipalTest(TestCase):
    """trigger_task attributes any decrypt done by a dispatched task to the
    silent ``system_cron`` principal (PR H). Probed DURING task execution —
    the middleware resets the principal to ``system`` on response, so it can't
    be observed after the request completes.
    """

    def setUp(self):
        self.client = APIClient()
        token = audit._PRINCIPAL.set("system")
        self.addCleanup(audit._PRINCIPAL.reset, token)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_dispatched_task_runs_under_system_cron(self, mock_verify):
        captured = {}

        def probe():
            captured["principal"] = audit.get_principal()
            return None

        with patch("apps.tenants.tasks.reset_daily_counters_task", side_effect=probe):
            response = self.client.post(
                "/api/v1/cron/trigger/reset_daily_counters/",
                data=b"",
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured.get("principal"), "system_cron")
