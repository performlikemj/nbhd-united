"""Tests for hibernation wake flow.

Regression guards for the wake-time image refresh that fixes the
"hibernated tenants come back stale" bug discovered 2026-04-26, plus
the cron-capture snapshot/seed fallback that fixes the silent
wake-chain breakage when the gateway returns 404 (Azure revision
inactive at hibernation time).
"""

from __future__ import annotations

from unittest.mock import call, patch

from django.db import OperationalError
from django.db.models import QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.cron.gateway_client import GatewayError
from apps.orchestrator.hibernation import _capture_tenant_cron_schedules, wake_hibernated_tenant
from apps.orchestrator.tool_policy import OPENCLAW_CURRENT_VERSION, openclaw_version_for_image_tag
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _apply_config_published(mock_publish) -> bool:
    """True if a config-apply task was published via the mocked publish_task."""
    return any(call.args and call.args[0] == "apply_single_tenant_config" for call in mock_publish.call_args_list)


class WakeHibernatedTenantImageRefreshTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Wake Refresh",
            telegram_chat_id=987654321,
        )
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-wake-test"
        self.tenant.container_fqdn = "oc-wake-test.internal"
        self.tenant.hibernated_at = timezone.now()
        self.tenant.save()

    @override_settings(
        OPENCLAW_IMAGE_TAG="newsha123",
        AZURE_ACR_SERVER="test.azurecr.io",
    )
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_refreshes_image_when_stale(
        self,
        mock_update_image,
        mock_wake,
        _mock_publish,
    ):
        """When tenant.container_image_tag != OPENCLAW_IMAGE_TAG, wake should
        push the new image (which auto-activates in single-revision mode) and
        skip the plain wake_container_app call.
        """
        self.tenant.container_image_tag = "oldsha456"
        self.tenant.save(update_fields=["container_image_tag"])

        result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        mock_update_image.assert_called_once_with(
            "oc-wake-test",
            "test.azurecr.io/nbhd-openclaw:newsha123",
        )
        mock_wake.assert_not_called()

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.container_image_tag, "newsha123")
        self.assertIsNone(self.tenant.hibernated_at)

    @override_settings(OPENCLAW_IMAGE_TAG="latest")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    def test_cron_wake_claims_atomically_before_azure(self, _mock_mount, mock_wake, _mock_publish):
        events = []
        original_update = QuerySet.update

        def recording_update(queryset, **updates):
            events.append(("update", updates, str(queryset.query)))
            return original_update(queryset, **updates)

        mock_wake.side_effect = lambda _container_id: events.append(("azure",))

        with patch.object(QuerySet, "update", autospec=True, side_effect=recording_update):
            result = wake_hibernated_tenant(self.tenant, cron_wake=True)

        self.assertTrue(result)
        claim = events[0]
        self.assertEqual(claim[0], "update")
        self.assertIsNone(claim[1]["hibernated_at"])
        self.assertIn("cron_wake_at", claim[1])
        self.assertIn("hibernated_at", claim[2])
        self.assertIn("IS NOT NULL", claim[2])
        self.assertEqual(events[1], ("azure",))

        self.tenant.refresh_from_db()
        self.assertIsNone(self.tenant.hibernated_at)
        self.assertIsNotNone(self.tenant.cron_wake_at)

    @override_settings(OPENCLAW_IMAGE_TAG="latest")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    def test_second_concurrent_wake_does_not_start_azure_twice(self, _mock_mount, mock_wake, _mock_publish):
        stale_tenant = Tenant.objects.get(id=self.tenant.id)

        self.assertTrue(wake_hibernated_tenant(self.tenant, cron_wake=True))
        self.assertTrue(wake_hibernated_tenant(stale_tenant, cron_wake=True))

        mock_wake.assert_called_once_with("oc-wake-test")

    @override_settings(OPENCLAW_IMAGE_TAG="latest")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    def test_failed_azure_wake_rolls_back_claim_for_retry(self, _mock_mount, mock_wake, _mock_publish):
        original_hibernated_at = self.tenant.hibernated_at
        original_cron_wake_at = timezone.now()
        self.tenant.cron_wake_at = original_cron_wake_at
        self.tenant.save(update_fields=["cron_wake_at"])
        mock_wake.side_effect = [RuntimeError("simulated Azure failure"), None]

        self.assertFalse(wake_hibernated_tenant(self.tenant, cron_wake=True))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.hibernated_at, original_hibernated_at)
        self.assertEqual(self.tenant.cron_wake_at, original_cron_wake_at)

        self.assertTrue(wake_hibernated_tenant(self.tenant, cron_wake=True))
        self.assertEqual(mock_wake.call_count, 2)

    @override_settings(OPENCLAW_IMAGE_TAG="latest")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    def test_already_awake_cron_wake_stamps_without_starting_azure(self, mock_mount, mock_wake, _mock_publish):
        self.tenant.hibernated_at = None
        self.tenant.cron_wake_at = None
        self.tenant.save(update_fields=["hibernated_at", "cron_wake_at"])

        self.assertTrue(wake_hibernated_tenant(self.tenant, cron_wake=True))

        mock_mount.assert_not_called()
        mock_wake.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.cron_wake_at)

    @override_settings(OPENCLAW_IMAGE_TAG="latest")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    def test_non_cron_wake_leaves_cron_wake_stamp_untouched(self, _mock_mount, _mock_wake, _mock_publish):
        existing_stamp = timezone.now()
        Tenant.objects.filter(id=self.tenant.id).update(cron_wake_at=existing_stamp)

        self.assertTrue(wake_hibernated_tenant(self.tenant))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.cron_wake_at, existing_stamp)

    @override_settings(
        OPENCLAW_IMAGE_TAG="newsha123",
        AZURE_ACR_SERVER="test.azurecr.io",
    )
    @patch("apps.tenants.middleware.set_rls_context")
    @patch("django.db.connection.close")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_post_azure_status_write_retries_stale_connection(
        self,
        _mock_update_image,
        _mock_publish,
        mock_close,
        mock_set_rls,
    ):
        self.tenant.container_image_tag = "oldsha456"
        self.tenant.save(update_fields=["container_image_tag"])
        original_update = QuerySet.update
        image_write_attempts = 0

        def stale_once(queryset, **updates):
            nonlocal image_write_attempts
            if updates.get("container_image_tag") == "newsha123":
                image_write_attempts += 1
                if image_write_attempts == 1:
                    raise OperationalError("simulated idle connection")
            return original_update(queryset, **updates)

        with patch.object(QuerySet, "update", autospec=True, side_effect=stale_once):
            result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        self.assertEqual(image_write_attempts, 2)
        mock_close.assert_called_once_with()
        self.assertEqual(mock_set_rls.call_args_list, [call(service_role=True)])

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.container_image_tag, "newsha123")

    @override_settings(
        OPENCLAW_IMAGE_TAG="samesha",
        AZURE_ACR_SERVER="test.azurecr.io",
    )
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_uses_plain_wake_when_image_already_current(
        self,
        mock_update_image,
        mock_ensure_mount,
        mock_wake,
        _mock_publish,
    ):
        """No image refresh when tenant is already on the desired tag —
        avoids creating an unnecessary new revision per wake. When the
        plugin-runtime-deps mount is already present, fall through to the
        plain wake call.
        """
        self.tenant.container_image_tag = "samesha"
        self.tenant.save(update_fields=["container_image_tag"])

        result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        mock_ensure_mount.assert_called_once_with("oc-wake-test")
        mock_wake.assert_called_once_with("oc-wake-test")
        mock_update_image.assert_not_called()

    @override_settings(
        OPENCLAW_IMAGE_TAG="samesha",
        AZURE_ACR_SERVER="test.azurecr.io",
    )
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=True)
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_skips_plain_wake_when_mount_was_added(
        self,
        mock_update_image,
        mock_ensure_mount,
        mock_wake,
        _mock_publish,
    ):
        """If ensure_plugin_runtime_deps_mount adds the mount, the resulting
        new revision auto-activates in single-revision mode — that wakes the
        container, so wake_container_app must not be called (would be a
        wasted second restart).
        """
        self.tenant.container_image_tag = "samesha"
        self.tenant.save(update_fields=["container_image_tag"])

        result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        mock_ensure_mount.assert_called_once_with("oc-wake-test")
        mock_wake.assert_not_called()
        mock_update_image.assert_not_called()

    @override_settings(
        OPENCLAW_IMAGE_TAG="latest",
        AZURE_ACR_SERVER="test.azurecr.io",
    )
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_uses_plain_wake_when_image_tag_is_latest(
        self,
        mock_update_image,
        mock_ensure_mount,
        mock_wake,
        _mock_publish,
    ):
        """The string 'latest' is the un-pinned default — never use it as a
        refresh target since it would re-pull the same floating tag every wake.
        """
        self.tenant.container_image_tag = "oldsha"
        self.tenant.save(update_fields=["container_image_tag"])

        wake_hibernated_tenant(self.tenant)

        mock_ensure_mount.assert_called_once_with("oc-wake-test")
        mock_wake.assert_called_once_with("oc-wake-test")
        mock_update_image.assert_not_called()


class OpenClawVersionForImageTagTest(TestCase):
    """The image-tag → schema-version mapping that keeps openclaw_version in
    lockstep with the running image (prevents the agents.defaults crash loop)."""

    def test_parses_version_prefix(self):
        self.assertEqual(openclaw_version_for_image_tag("2026.5.28-755d789"), "2026.5.28")
        self.assertEqual(openclaw_version_for_image_tag("2026.4.25-abc123"), "2026.4.25")

    def test_falls_back_to_current_for_bare_sha_or_latest(self):
        self.assertEqual(openclaw_version_for_image_tag("4a969adbbe6c3e"), OPENCLAW_CURRENT_VERSION)
        self.assertEqual(openclaw_version_for_image_tag("latest"), OPENCLAW_CURRENT_VERSION)
        self.assertEqual(openclaw_version_for_image_tag(""), OPENCLAW_CURRENT_VERSION)


class WakeConfigSchemaSyncTest(TestCase):
    """Regression guards for the 2026-06-17 crash-loop incident: a tenant
    woken onto a newer image kept a stale ``openclaw_version`` (config schema)
    and/or a missing ``openclaw.json``, so the new image rejected the config
    (``agents.defaults: Invalid input``) or found no config and crash-looped.
    Wake must sync the version to the image and force a config write for the
    invisible ``pending==config`` states.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Wake Schema", telegram_chat_id=55512345)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-schema-test"
        self.tenant.container_fqdn = "oc-schema-test.internal"
        self.tenant.hibernated_at = timezone.now()
        self.tenant.save()

    @override_settings(OPENCLAW_IMAGE_TAG="2026.5.28-755d789", AZURE_ACR_SERVER="test.azurecr.io")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_syncs_version_and_forces_config_apply_when_stale(self, mock_update_image, _mock_wake, mock_publish):
        """Image refresh onto a newer tag must move openclaw_version with it
        and queue a config regen even when pending==config."""
        Tenant.objects.filter(id=self.tenant.id).update(
            openclaw_version="2026.4.25",
            container_image_tag="2026.4.25-oldsha",
            config_version=3,
            pending_config_version=3,
        )
        self.tenant.refresh_from_db()

        result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        mock_update_image.assert_called_once()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.openclaw_version, "2026.5.28")
        self.assertEqual(self.tenant.container_image_tag, "2026.5.28-755d789")
        # pending bumped past config so the apply actually writes
        self.assertGreater(self.tenant.pending_config_version, self.tenant.config_version)
        self.assertTrue(_apply_config_published(mock_publish))

    @override_settings(OPENCLAW_IMAGE_TAG="2026.5.28-755d789", AZURE_ACR_SERVER="test.azurecr.io")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_forces_config_apply_when_never_configured(
        self, mock_update_image, _mock_mount, _mock_wake, mock_publish
    ):
        """config_version==0 (never version-applied) forces a config write on
        wake even with no image refresh — covers a failed provision-time seed
        that left the share with no openclaw.json."""
        Tenant.objects.filter(id=self.tenant.id).update(
            openclaw_version="2026.5.28",
            container_image_tag="2026.5.28-755d789",  # already current → no image refresh
            config_version=0,
            pending_config_version=0,
        )
        self.tenant.refresh_from_db()

        result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        mock_update_image.assert_not_called()  # image already current
        self.tenant.refresh_from_db()
        self.assertGreater(self.tenant.pending_config_version, self.tenant.config_version)
        self.assertTrue(_apply_config_published(mock_publish))

    @override_settings(OPENCLAW_IMAGE_TAG="2026.5.28-755d789", AZURE_ACR_SERVER="test.azurecr.io")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.wake_container_app")
    @patch("apps.orchestrator.azure_client.ensure_plugin_runtime_deps_mount", return_value=False)
    @patch("apps.orchestrator.azure_client.update_container_image")
    def test_wake_no_forced_apply_for_healthy_tenant(self, _mock_update_image, _mock_mount, _mock_wake, mock_publish):
        """A configured tenant already on the current version/image must NOT
        get a forced config apply (avoid needless regen churn every wake)."""
        Tenant.objects.filter(id=self.tenant.id).update(
            openclaw_version="2026.5.28",
            container_image_tag="2026.5.28-755d789",
            config_version=7,
            pending_config_version=7,
        )
        self.tenant.refresh_from_db()

        result = wake_hibernated_tenant(self.tenant)

        self.assertTrue(result)
        self.assertFalse(_apply_config_published(mock_publish))


class CaptureTenantCronSchedulesFallbackTest(TestCase):
    """Regression guards for the snapshot/seed fallback in
    ``_capture_tenant_cron_schedules``.

    The bug this prevents: when the per-tenant container's revision is
    inactive at the moment of hibernation, ``cron.list`` over the
    gateway returns Azure's HTML 404. Pre-fix, the function silently
    returned ``[]``, ``_schedule_next_cron_wake`` skipped, and the
    tenant wedged in hibernation forever. Post-fix, snapshot/seed
    fallback ensures wake is always armed.
    """

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Fallback Test",
            telegram_chat_id=123456789,
        )
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-fallback-test"
        self.tenant.container_fqdn = "oc-fallback-test.internal"
        self.tenant.save()

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_uses_live_response_when_gateway_succeeds(self, mock_invoke):
        live_jobs = [
            {"name": "Morning Briefing", "schedule": {"expr": "0 7 * * *", "tz": "UTC"}, "enabled": True},
            {"name": "Heartbeat", "schedule": {"expr": "*/15 * * * *", "tz": "UTC"}, "enabled": True},
        ]
        mock_invoke.return_value = {"jobs": live_jobs}

        result = _capture_tenant_cron_schedules(self.tenant)

        self.assertEqual(result, live_jobs)
        mock_invoke.assert_called_once_with(self.tenant, "cron.list", {"includeDisabled": False})

        # Snapshot persisted on success
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.cron_jobs_snapshot)
        self.assertEqual(self.tenant.cron_jobs_snapshot["jobs"], live_jobs)
        self.assertIn("snapshot_at", self.tenant.cron_jobs_snapshot)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_falls_back_to_snapshot_when_gateway_fails(self, mock_invoke):
        snapshot_jobs = [
            {"name": "Morning Briefing", "schedule": {"expr": "0 7 * * *", "tz": "UTC"}, "enabled": True},
            {"name": "Disabled Job", "schedule": {"expr": "0 9 * * *", "tz": "UTC"}, "enabled": False},
        ]
        self.tenant.cron_jobs_snapshot = {
            "jobs": snapshot_jobs,
            "snapshot_at": timezone.now().isoformat(),
        }
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        mock_invoke.side_effect = GatewayError("404: <!DOCTYPE html>", status_code=404)

        result = _capture_tenant_cron_schedules(self.tenant)

        # Returns only enabled jobs from snapshot — matches live cron.list semantics
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Morning Briefing")
        mock_invoke.assert_called_once()

    @patch("apps.orchestrator.config_generator.build_cron_seed_jobs")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_falls_back_to_seed_when_gateway_fails_and_snapshot_empty(self, mock_invoke, mock_seed):
        mock_invoke.side_effect = GatewayError("404: <!DOCTYPE html>", status_code=404)

        seed_jobs = [
            {"name": "Morning Briefing", "schedule": {"expr": "0 7 * * *", "tz": "UTC"}, "enabled": True},
            {"name": "Evening Check-in", "schedule": {"expr": "0 21 * * *", "tz": "UTC"}, "enabled": True},
        ]
        mock_seed.return_value = seed_jobs

        # Snapshot left empty (default-dict {})
        self.tenant.cron_jobs_snapshot = {}
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        result = _capture_tenant_cron_schedules(self.tenant)

        self.assertEqual(result, seed_jobs)
        mock_seed.assert_called_once_with(self.tenant)

    @patch("apps.orchestrator.config_generator.build_cron_seed_jobs")
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_returns_empty_when_all_fallbacks_fail(self, mock_invoke, mock_seed):
        mock_invoke.side_effect = GatewayError("502: bad_gateway", status_code=502)
        mock_seed.side_effect = RuntimeError("seed broken")

        self.tenant.cron_jobs_snapshot = {}
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        result = _capture_tenant_cron_schedules(self.tenant)

        self.assertEqual(result, [])
        mock_invoke.assert_called_once()
        mock_seed.assert_called_once()

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_no_container_fqdn_returns_empty_without_calling_gateway(self, mock_invoke):
        self.tenant.container_fqdn = ""
        self.tenant.save(update_fields=["container_fqdn"])

        result = _capture_tenant_cron_schedules(self.tenant)

        self.assertEqual(result, [])
        mock_invoke.assert_not_called()
