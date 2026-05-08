"""Tests for the atomic fleet bump endpoint and its per-tenant task."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


@override_settings(DEPLOY_SECRET="test-deploy-secret", OPENCLAW_IMAGE_TAG="testsha")
class RolloutAtomicBumpEndpointTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.tenant = create_tenant(display_name="Atomic Test", telegram_chat_id=999111222)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-atomic-test"
        self.tenant.container_fqdn = "oc-atomic-test.internal"
        self.tenant.openclaw_version = "2026.0.0"  # arbitrary < target
        self.tenant.save()
        cache.delete("rollout_atomic_bump:in_flight")

    def _post(self, body=None, secret="test-deploy-secret"):
        return self.client.post(
            "/api/cron/rollout-atomic-bump/",
            data=json.dumps(body or {}),
            content_type="application/json",
            HTTP_X_DEPLOY_SECRET=secret,
        )

    def test_unauthorized_without_secret(self):
        resp = self.client.post(
            "/api/cron/rollout-atomic-bump/",
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_unauthorized_wrong_secret(self):
        resp = self._post(body={}, secret="wrong-secret")
        self.assertEqual(resp.status_code, 401)

    def test_rejects_latest_image_tag(self):
        resp = self._post(body={"image_tag": "latest"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("latest", resp.json()["error"])

    @patch("apps.cron.views.publish_batch", return_value=0)
    def test_dry_run_does_not_publish(self, mock_publish):
        resp = self._post(body={"dry_run": True, "oc_version": "2026.99.0", "image_tag": "futuresha"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["queued"], 0)
        self.assertIn(str(self.tenant.id), body["tenant_ids"])
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_batch", return_value=1)
    def test_queues_task_for_eligible_tenant(self, mock_publish):
        resp = self._post(body={"oc_version": "2026.99.0", "image_tag": "futuresha"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["queued"], 1)
        self.assertEqual(body["oc_version"], "2026.99.0")
        self.assertEqual(body["image_tag"], "futuresha")
        # publish_batch invoked with the per-tenant atomic-bump task
        args, _ = mock_publish.call_args
        tasks = args[0]
        self.assertEqual(len(tasks), 1)
        task_name, task_args, _ = tasks[0]
        self.assertEqual(task_name, "bump_openclaw_atomic_per_tenant")
        self.assertEqual(task_args[0], str(self.tenant.id))
        self.assertEqual(task_args[1], "2026.99.0")
        self.assertEqual(task_args[2], "futuresha")

    @patch("apps.cron.publish.publish_batch", return_value=0)
    def test_skips_already_bumped_tenants(self, mock_publish):
        self.tenant.openclaw_version = "2026.99.0"
        self.tenant.container_image_tag = "futuresha"
        self.tenant.save()

        resp = self._post(body={"oc_version": "2026.99.0", "image_tag": "futuresha"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["queued"], 0)
        self.assertEqual(body["tenant_ids"], [])
        mock_publish.assert_not_called()

    @patch("apps.cron.publish.publish_batch", return_value=1)
    def test_canary_mode_filters_to_single_tenant(self, mock_publish):
        # Second tenant that should NOT be bumped
        other = create_tenant(display_name="Other", telegram_chat_id=999111333)
        other.status = Tenant.Status.ACTIVE
        other.container_id = "oc-other"
        other.openclaw_version = "2026.0.0"
        other.save()

        resp = self._post(
            body={
                "oc_version": "2026.99.0",
                "image_tag": "futuresha",
                "tenant_id": str(self.tenant.id),
            }
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["tenant_ids"], [str(self.tenant.id)])

    def test_concurrent_invocation_returns_409(self):
        # Hold the lock to simulate an in-flight rollout
        cache.set("rollout_atomic_bump:in_flight", "1", timeout=30)
        try:
            resp = self._post(body={"oc_version": "2026.99.0", "image_tag": "futuresha"})
            self.assertEqual(resp.status_code, 409)
        finally:
            cache.delete("rollout_atomic_bump:in_flight")


@override_settings(DEPLOY_SECRET="test-deploy-secret")
class AtomicBumpStatusTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.tenant = create_tenant(display_name="Status Test", telegram_chat_id=998111000)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-status-test"
        self.tenant.openclaw_version = "2026.5.7"
        self.tenant.container_image_tag = "abc123"
        self.tenant.save()

    def test_unauthorized(self):
        resp = self.client.get("/api/cron/atomic-bump-status/")
        self.assertEqual(resp.status_code, 401)

    def test_lists_active_tenants(self):
        resp = self.client.get(
            "/api/cron/atomic-bump-status/",
            HTTP_X_DEPLOY_SECRET="test-deploy-secret",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("tenants", body)
        ids = [t["tenant_id"] for t in body["tenants"]]
        self.assertIn(str(self.tenant.id), ids)
        my_row = next(t for t in body["tenants"] if t["tenant_id"] == str(self.tenant.id))
        self.assertEqual(my_row["oc_version"], "2026.5.7")
        self.assertEqual(my_row["image_tag"], "abc123")
        self.assertFalse(my_row["hibernated"])


class BumpOpenclawAtomicPerTenantTaskTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Per-Tenant Task", telegram_chat_id=997111000)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-per-tenant"
        self.tenant.openclaw_version = "2026.0.0"
        self.tenant.save()

    @patch("apps.orchestrator.tasks.bump_openclaw_version_for_tenant")
    def test_dispatches_to_service_function(self, mock_bump):
        from apps.orchestrator.tasks import bump_openclaw_atomic_per_tenant_task

        bump_openclaw_atomic_per_tenant_task(str(self.tenant.id), "2026.99.0", "futuresha")
        mock_bump.assert_called_once()
        args, _ = mock_bump.call_args
        self.assertEqual(args[1], "2026.99.0")
        self.assertEqual(args[2], "futuresha")

    @patch("apps.orchestrator.tasks.bump_openclaw_version_for_tenant")
    def test_skips_tenant_already_at_target(self, mock_bump):
        from apps.orchestrator.tasks import bump_openclaw_atomic_per_tenant_task

        self.tenant.openclaw_version = "2026.99.0"
        self.tenant.container_image_tag = "futuresha"
        self.tenant.save()

        bump_openclaw_atomic_per_tenant_task(str(self.tenant.id), "2026.99.0", "futuresha")
        mock_bump.assert_not_called()

    @patch("apps.orchestrator.tasks.bump_openclaw_version_for_tenant")
    def test_skips_suspended_tenant(self, mock_bump):
        from apps.orchestrator.tasks import bump_openclaw_atomic_per_tenant_task

        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.save()

        bump_openclaw_atomic_per_tenant_task(str(self.tenant.id), "2026.99.0", "futuresha")
        mock_bump.assert_not_called()

    @patch("apps.orchestrator.tasks.bump_openclaw_version_for_tenant")
    def test_no_op_when_tenant_deleted(self, mock_bump):
        from apps.orchestrator.tasks import bump_openclaw_atomic_per_tenant_task

        bogus_id = "00000000-0000-0000-0000-000000000000"
        bump_openclaw_atomic_per_tenant_task(bogus_id, "2026.99.0", "futuresha")
        mock_bump.assert_not_called()
