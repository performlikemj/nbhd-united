"""Tests for image auto-update behavior in apply_pending_configs."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User


def _create_tenant_with_state(
    *,
    user_suffix: int,
    active: bool = True,
    config_version: int = 0,
    pending_config_version: int = 0,
    last_message_at=None,
    has_container: bool = True,
    container_image_tag: str = "",
):
    user = User.objects.create_user(
        username=f"image-user-{user_suffix}",
        password="testpass123",
    )
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE if active else Tenant.Status.PENDING,
        model_tier=Tenant.ModelTier.STARTER,
        container_id="oc-test" if has_container else "",
        container_fqdn="oc-test.internal.azurecontainerapps.io" if has_container else "",
        container_image_tag=container_image_tag,
        config_version=config_version,
        pending_config_version=pending_config_version,
        last_message_at=last_message_at,
    )
    return tenant


def _extract_batch_tasks(mock_batch, task_name):
    """Extract tasks of a given name from all publish_batch mock calls."""
    results = []
    for call in mock_batch.call_args_list:
        batch_tasks = call[0][0]
        results.extend(t for t in batch_tasks if t[0] == task_name)
    return results


def _batch_return_len(tasks, **kwargs):
    """Mock side_effect that returns len(tasks) to simulate success."""
    return len(tasks)


@override_settings(OPENCLAW_IMAGE_TAG="abc123", AZURE_ACR_SERVER="nbhdunited.azurecr.io")
class ApplyPendingConfigsImageTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=_batch_return_len)
    def test_apply_pending_configs_enqueues_stale_images(
        self,
        mock_batch,
        _mock_verify,
    ):
        now = timezone.now()
        _create_tenant_with_state(
            user_suffix=1,
            pending_config_version=1,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="oldtag",
        )

        _create_tenant_with_state(
            user_suffix=2,
            pending_config_version=0,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="oldtag",
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["config_enqueued"], 1)
        self.assertEqual(body["image_enqueued"], 2)
        self.assertEqual(body["image_failed"], 0)

        config_calls = _extract_batch_tasks(mock_batch, "apply_single_tenant_config")
        image_calls = _extract_batch_tasks(mock_batch, "apply_single_tenant_image")
        self.assertEqual(len(config_calls), 1)
        self.assertEqual(len(image_calls), 2)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=_batch_return_len)
    def test_active_non_idle_tenants_are_not_image_enqueued(
        self,
        mock_batch,
        _mock_verify,
    ):
        now = timezone.now()
        _create_tenant_with_state(
            user_suffix=1,
            pending_config_version=0,
            config_version=0,
            last_message_at=now - timedelta(minutes=5),
            container_image_tag="oldtag",
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["image_enqueued"], 0)

        image_calls = _extract_batch_tasks(mock_batch, "apply_single_tenant_image")
        self.assertEqual(len(image_calls), 0)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=_batch_return_len)
    def test_tenants_already_on_desired_tag_are_skipped(
        self,
        mock_batch,
        _mock_verify,
    ):
        now = timezone.now()
        _create_tenant_with_state(
            user_suffix=1,
            pending_config_version=0,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="abc123",
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["image_enqueued"], 0)

        image_calls = _extract_batch_tasks(mock_batch, "apply_single_tenant_image")
        self.assertEqual(len(image_calls), 0)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=_batch_return_len)
    def test_batch_collects_all_tenant_tasks(
        self,
        mock_batch,
        _mock_verify,
    ):
        now = timezone.now()
        _create_tenant_with_state(
            user_suffix=1,
            pending_config_version=1,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="oldtag",
        )
        _create_tenant_with_state(
            user_suffix=2,
            pending_config_version=1,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="oldtag",
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        # With batch, all tasks are collected then published together.
        self.assertEqual(body["config_enqueued"], 2)
        self.assertEqual(body["config_failed"], 0)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.publish.publish_batch", side_effect=Exception("QStash down"))
    def test_batch_failure_reports_all_tasks_as_failed(
        self,
        mock_batch,
        _mock_verify,
    ):
        now = timezone.now()
        _create_tenant_with_state(
            user_suffix=1,
            pending_config_version=1,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="oldtag",
        )
        _create_tenant_with_state(
            user_suffix=2,
            pending_config_version=1,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="oldtag",
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        # Batch is all-or-nothing — on failure, all counts go to *_failed.
        self.assertEqual(body["config_enqueued"], 0)
        self.assertEqual(body["config_failed"], 2)
        self.assertEqual(body["image_enqueued"], 0)
        self.assertEqual(body["image_failed"], 2)
        self.assertEqual(body["batch_enqueued"], 0)
