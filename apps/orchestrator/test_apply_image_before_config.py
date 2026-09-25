"""Regression coverage for the image-before-config apply ordering.

Incident 2026-07-03: ``apply_pending_configs`` wrote a tenant's regenerated
``openclaw.json`` to the file share ~45s BEFORE the container image update
landed. The config referenced a plugin dir that exists only in the new image;
the old image (OpenClaw 2026.5.28 re-reads a changed config on the share live)
rejected it and fell back to ``openclaw.json.last-good`` — so even the new
image then booted on stale config.

The fix routes config-pending image-stale tenants through
``apply_single_tenant_image_task``, which updates the image FIRST and writes
the config AFTER, then stamps ``config_version``. These tests pin that
ordering, the failure semantics (no config write / no stamp if the image push
fails), and same-tag idempotency.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.tasks import apply_single_tenant_image_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

TARGET_TAG = "2026.5.28-def456"


class ApplyImageBeforeConfigTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="ImageBeforeConfig", telegram_chat_id=515151)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-ibc-test"
        self.tenant.container_fqdn = "oc-ibc-test.internal"
        self.tenant.container_image_tag = "oldtag"
        self.tenant.config_version = 1
        self.tenant.pending_config_version = 2
        self.tenant.save()

    def test_image_is_written_before_config(self):
        """update_container_image must be called before update_tenant_config."""
        order: list[str] = []

        with (
            patch(
                "apps.orchestrator.azure_client.update_container_image",
                side_effect=lambda *a, **k: order.append("image"),
            ),
            patch(
                "apps.orchestrator.services.update_tenant_config",
                side_effect=lambda *a, **k: order.append("config"),
            ),
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}),
            patch("apps.cron.publish.publish_task"),
        ):
            apply_single_tenant_image_task(str(self.tenant.id), TARGET_TAG)

        self.assertEqual(order, ["image", "config"])

    def test_config_version_stamped_after_successful_image_and_config(self):
        """A pending config queued alongside the image bump is marked applied
        by the image task once the image + config writes both succeed."""
        with (
            patch("apps.orchestrator.azure_client.update_container_image"),
            patch("apps.orchestrator.services.update_tenant_config"),
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}),
            patch("apps.cron.publish.publish_task"),
        ):
            apply_single_tenant_image_task(str(self.tenant.id), TARGET_TAG)

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.container_image_tag, TARGET_TAG)
        self.assertEqual(self.tenant.config_version, 2)
        self.assertIsNotNone(self.tenant.applied_model_at)

    def test_image_failure_skips_config_write_and_does_not_stamp(self):
        """If the image push fails, the config must NOT be written and the
        version must NOT be stamped — pending_config_version stays ahead so the
        next apply_pending_configs cycle retries."""
        with (
            patch(
                "apps.orchestrator.azure_client.update_container_image",
                side_effect=RuntimeError("azure revision push failed"),
            ),
            patch("apps.orchestrator.services.update_tenant_config") as mock_cfg,
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}),
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_image_task(str(self.tenant.id), TARGET_TAG)

        mock_cfg.assert_not_called()
        mock_publish.assert_not_called()  # bailed before Phase 3 restore scheduling

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.container_image_tag, "oldtag")  # image not recorded
        self.assertEqual(self.tenant.config_version, 1)  # not stamped
        self.assertEqual(self.tenant.pending_config_version, 2)  # stays ahead for retry

    def test_config_still_stamped_when_pending_equals_config(self):
        """Image-stale but config-current tenant: the post-image config write
        is a no-op version advance and must not error."""
        self.tenant.pending_config_version = 1  # == config_version
        self.tenant.save(update_fields=["pending_config_version"])

        with (
            patch("apps.orchestrator.azure_client.update_container_image"),
            patch("apps.orchestrator.services.update_tenant_config") as mock_cfg,
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={"jobs": []}),
            patch("apps.cron.publish.publish_task"),
        ):
            apply_single_tenant_image_task(str(self.tenant.id), TARGET_TAG)

        mock_cfg.assert_called_once()  # config still regenerated against new schema
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.container_image_tag, TARGET_TAG)
        self.assertEqual(self.tenant.config_version, 1)

    def test_idempotent_when_already_on_target_tag(self):
        """A duplicate delivery for a tenant already on the target tag must not
        re-push the image (same-tag re-bump wedges single-revision apps) or
        rewrite config."""
        self.tenant.container_image_tag = TARGET_TAG
        self.tenant.save(update_fields=["container_image_tag"])

        with (
            patch("apps.orchestrator.azure_client.update_container_image") as mock_img,
            patch("apps.orchestrator.services.update_tenant_config") as mock_cfg,
            patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_gw,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_image_task(str(self.tenant.id), TARGET_TAG)

        mock_img.assert_not_called()
        mock_cfg.assert_not_called()
        mock_gw.assert_not_called()
        mock_publish.assert_not_called()
