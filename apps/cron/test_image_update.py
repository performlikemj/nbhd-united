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


@override_settings(OPENCLAW_IMAGE_TAG="abc123", AZURE_ACR_SERVER="nbhdunited.azurecr.io")
class ApplyPendingConfigsImageTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.views.update_container_image")
    @patch("apps.cron.views.update_tenant_config")
    def test_apply_pending_configs_updates_stale_images_when_tag_set(
        self,
        mock_update_tenant_config,
        mock_update_container_image,
        _mock_verify,
    ):
        now = timezone.now()
        tenant_stale_idle = _create_tenant_with_state(
            user_suffix=1,
            pending_config_version=1,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="oldtag",
        )

        # image stale but no config updates needed
        tenant_image_only = _create_tenant_with_state(
            user_suffix=2,
            pending_config_version=0,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="oldtag",
        )

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["image_updated"], 2)
        self.assertEqual(body["image_failed"], 0)

        desired_image = "nbhdunited.azurecr.io/nbhd-openclaw:abc123"
        self.assertEqual(mock_update_tenant_config.call_count, 1)
        self.assertEqual(mock_update_container_image.call_count, 2)
        mock_update_container_image.assert_any_call(tenant_stale_idle.container_id, desired_image)
        mock_update_container_image.assert_any_call(tenant_image_only.container_id, desired_image)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.views.update_container_image")
    @patch("apps.cron.views.update_tenant_config")
    def test_active_non_idle_tenants_are_not_image_updated(
        self,
        mock_update_tenant_config,
        mock_update_container_image,
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
        self.assertEqual(body["image_updated"], 0)
        mock_update_container_image.assert_not_called()
        self.assertEqual(mock_update_tenant_config.call_count, 0)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.views.update_container_image")
    @patch("apps.cron.views.update_tenant_config")
    def test_tenants_already_on_desired_tag_are_skipped(
        self,
        mock_update_tenant_config,
        mock_update_container_image,
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
        self.assertEqual(body["image_updated"], 0)
        mock_update_container_image.assert_not_called()
        self.assertEqual(mock_update_tenant_config.call_count, 0)

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.cron.views.update_container_image")
    @patch("apps.cron.views.update_tenant_config")
    def test_image_update_failures_do_not_block_config_updates(
        self,
        mock_update_tenant_config,
        mock_update_container_image,
        _mock_verify,
    ):
        now = timezone.now()
        tenant = _create_tenant_with_state(
            user_suffix=1,
            pending_config_version=1,
            config_version=0,
            last_message_at=now - timedelta(minutes=20),
            container_image_tag="oldtag",
        )

        # image update fails but config update succeeds
        mock_update_container_image.side_effect = Exception("revision failed")

        response = self.client.post("/api/v1/cron/apply-pending-configs/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["failed"], 0)
        self.assertEqual(body["image_updated"], 0)
        self.assertEqual(body["image_failed"], 1)
        mock_update_tenant_config.assert_called_once_with(str(tenant.id))
        tenant.refresh_from_db()
        self.assertEqual(tenant.container_image_tag, "oldtag")
