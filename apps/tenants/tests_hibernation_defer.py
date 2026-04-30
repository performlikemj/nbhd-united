"""Tests for ``apply_or_defer_gateway_call`` and the wired endpoints.

Covers:
- Helper unit behaviour: awake/hibernated/awake-fallthrough on
  container-unavailable GatewayError, and that other exceptions bubble.
- Integration: each wired endpoint defers cleanly while hibernated and
  attempts no Azure call.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.cron.gateway_client import GatewayError
from apps.integrations.models import Integration
from apps.orchestrator.hibernation import DEFERRED, apply_or_defer_gateway_call
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_active_tenant(*, hibernated: bool, chat_id: int = 999_000_001) -> Tenant:
    """Create a tenant that looks like a real provisioned subscriber."""
    tenant = create_tenant(display_name="Hibernation Test", telegram_chat_id=chat_id)
    tenant.status = Tenant.Status.ACTIVE
    tenant.container_id = "oc-test-tenant"
    tenant.container_fqdn = "oc-test-tenant.test.example.com"
    tenant.hibernated_at = timezone.now() if hibernated else None
    tenant.save(
        update_fields=[
            "status",
            "container_id",
            "container_fqdn",
            "hibernated_at",
            "updated_at",
        ]
    )
    return tenant


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class ApplyOrDeferAwakeTest(TestCase):
    """Awake tenant runs the callable and returns its result."""

    def setUp(self):
        self.tenant = _make_active_tenant(hibernated=False, chat_id=999_000_010)
        self.starting_pending = self.tenant.pending_config_version

    @mock.patch("apps.orchestrator.hibernation._write_deferred_state")
    def test_awake_invokes_callable_and_returns_result(self, mock_defer):
        callable_mock = mock.Mock(return_value="ok-result")

        result = apply_or_defer_gateway_call(
            self.tenant, callable_mock, label="unit.awake"
        )

        callable_mock.assert_called_once_with()
        self.assertEqual(result, "ok-result")
        mock_defer.assert_not_called()

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pending_config_version, self.starting_pending)


class ApplyOrDeferHibernatedTest(TestCase):
    """Hibernated tenant skips the callable, bumps pending, writes file share."""

    def setUp(self):
        self.tenant = _make_active_tenant(hibernated=True, chat_id=999_000_011)
        self.starting_pending = self.tenant.pending_config_version

    @mock.patch("apps.orchestrator.azure_client.upload_config_to_file_share")
    @mock.patch("apps.orchestrator.config_generator.generate_openclaw_config")
    @mock.patch("apps.orchestrator.config_generator.config_to_json", return_value="{}")
    def test_hibernated_skips_callable_and_defers(
        self, mock_to_json, mock_gen, mock_upload
    ):
        mock_gen.return_value = {"agent": {"name": "test"}}
        callable_mock = mock.Mock(return_value="should-not-run")

        result = apply_or_defer_gateway_call(
            self.tenant, callable_mock, label="unit.hibernated"
        )

        callable_mock.assert_not_called()
        self.assertIs(result, DEFERRED)

        self.tenant.refresh_from_db()
        self.assertEqual(
            self.tenant.pending_config_version, self.starting_pending + 1
        )

        mock_gen.assert_called_once()
        mock_upload.assert_called_once()
        upload_args, _ = mock_upload.call_args
        self.assertEqual(upload_args[0], str(self.tenant.id))


class ApplyOrDeferAwakeFallthroughTest(TestCase):
    """Awake-path GatewayError that looks like 'container unavailable' falls through."""

    def setUp(self):
        self.tenant = _make_active_tenant(hibernated=False, chat_id=999_000_012)
        self.starting_pending = self.tenant.pending_config_version

    @mock.patch("apps.orchestrator.azure_client.upload_config_to_file_share")
    @mock.patch("apps.orchestrator.config_generator.generate_openclaw_config")
    @mock.patch("apps.orchestrator.config_generator.config_to_json", return_value="{}")
    def test_unavailable_gateway_error_defers(self, mock_to_json, mock_gen, mock_upload):
        mock_gen.return_value = {"agent": {"name": "test"}}
        # Build a 502-shaped GatewayError that is_container_unavailable_error
        # recognises (status_code in {404, 502, 503, 504}).
        exc = GatewayError("boom")
        exc.status_code = 502
        callable_mock = mock.Mock(side_effect=exc)

        result = apply_or_defer_gateway_call(
            self.tenant, callable_mock, label="unit.unavailable"
        )

        callable_mock.assert_called_once_with()
        self.assertIs(result, DEFERRED)

        self.tenant.refresh_from_db()
        self.assertEqual(
            self.tenant.pending_config_version, self.starting_pending + 1
        )
        mock_upload.assert_called_once()

    def test_real_gateway_error_bubbles_untouched(self):
        # status_code=400 is a real application error, not a hibernation race.
        # _UNAVAILABLE_STATUS_CODES = {404, 502, 503, 504}; 400 is excluded
        # explicitly by is_container_unavailable_error when code is set.
        exc = GatewayError("bad request")
        exc.status_code = 400
        callable_mock = mock.Mock(side_effect=exc)

        with self.assertRaises(GatewayError):
            apply_or_defer_gateway_call(
                self.tenant, callable_mock, label="unit.real-error"
            )

        self.tenant.refresh_from_db()
        self.assertEqual(
            self.tenant.pending_config_version, self.starting_pending
        )

    def test_non_gateway_exception_bubbles_untouched(self):
        # Catches must be narrow — a ValueError must not be swallowed.
        callable_mock = mock.Mock(side_effect=ValueError("logic bug"))

        with self.assertRaises(ValueError):
            apply_or_defer_gateway_call(
                self.tenant, callable_mock, label="unit.non-gateway"
            )

        self.tenant.refresh_from_db()
        self.assertEqual(
            self.tenant.pending_config_version, self.starting_pending
        )


# ---------------------------------------------------------------------------
# Integration tests — one per wired endpoint
# ---------------------------------------------------------------------------


class HeartbeatEndpointDeferTest(TestCase):
    """PATCH /api/v1/tenants/heartbeat/ defers cleanly while hibernated."""

    def setUp(self):
        self.tenant = _make_active_tenant(hibernated=True, chat_id=999_000_020)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    @mock.patch("apps.orchestrator.azure_client.upload_config_to_file_share")
    @mock.patch("apps.orchestrator.config_generator.generate_openclaw_config")
    @mock.patch("apps.orchestrator.services.sync_heartbeat_cron")
    @mock.patch("apps.orchestrator.services.update_tenant_config")
    def test_hibernated_patch_returns_pending_and_skips_gateway(
        self, mock_update_cfg, mock_sync_hb, mock_gen, mock_upload
    ):
        mock_gen.return_value = {"agent": {"name": "test"}}

        response = self.client.patch(
            "/api/v1/tenants/heartbeat/",
            {"start_hour": 9},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["applied"], "pending")
        self.assertEqual(response.json()["start_hour"], 9)

        # No gateway side-effects ran.
        mock_update_cfg.assert_not_called()
        mock_sync_hb.assert_not_called()

        # File-share write happened (Azure Storage path is OK while hibernated).
        self.assertTrue(mock_upload.called)

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.heartbeat_start_hour, 9)
        # Both deferred call sites bump pending; expect at least one bump.
        self.assertGreaterEqual(self.tenant.pending_config_version, 1)


class IntegrationsDisconnectDeferTest(TestCase):
    """POST /api/v1/integrations/<id>/disconnect/ defers while hibernated."""

    def setUp(self):
        self.tenant = _make_active_tenant(hibernated=True, chat_id=999_000_021)
        self.integration = Integration.objects.create(
            tenant=self.tenant,
            provider="google",
            status="active",
            provider_email="user@example.com",
            scopes=[],
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    @mock.patch("apps.integrations.services.disconnect_integration")
    @mock.patch("apps.orchestrator.azure_client.upload_config_to_file_share")
    @mock.patch("apps.orchestrator.config_generator.generate_openclaw_config")
    @mock.patch("apps.orchestrator.services.update_tenant_config")
    def test_hibernated_disconnect_returns_pending_and_skips_gateway(
        self, mock_update_cfg, mock_gen, mock_upload, mock_disconnect
    ):
        mock_gen.return_value = {"agent": {"name": "test"}}

        response = self.client.post(
            f"/api/v1/integrations/{self.integration.id}/disconnect/",
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "disconnected")
        self.assertEqual(body["applied"], "pending")

        mock_update_cfg.assert_not_called()
        self.assertTrue(mock_upload.called)


class ProfileTimezoneEndpointDeferTest(TestCase):
    """PATCH /api/v1/tenants/profile/ timezone change defers while hibernated."""

    def setUp(self):
        self.tenant = _make_active_tenant(hibernated=True, chat_id=999_000_022)
        self.tenant.user.timezone = "UTC"
        self.tenant.user.save(update_fields=["timezone"])
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    @mock.patch("apps.cron.gateway_client.invoke_gateway_tool")
    @mock.patch("apps.orchestrator.azure_client.upload_config_to_file_share")
    @mock.patch("apps.orchestrator.config_generator.generate_openclaw_config")
    @mock.patch("apps.orchestrator.services.update_tenant_config")
    def test_hibernated_timezone_change_defers(
        self, mock_update_cfg, mock_gen, mock_upload, mock_gateway
    ):
        mock_gen.return_value = {"agent": {"name": "test"}}

        response = self.client.patch(
            "/api/v1/tenants/profile/",
            {"timezone": "America/New_York"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["applied"], "pending")

        mock_update_cfg.assert_not_called()
        mock_gateway.assert_not_called()
        self.assertTrue(mock_upload.called)

        self.tenant.user.refresh_from_db()
        self.assertEqual(self.tenant.user.timezone, "America/New_York")


class ProfileLocationEndpointDeferTest(TestCase):
    """PATCH /api/v1/tenants/profile/ location change defers while hibernated."""

    def setUp(self):
        self.tenant = _make_active_tenant(hibernated=True, chat_id=999_000_023)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    @mock.patch("apps.orchestrator.azure_client.upload_config_to_file_share")
    @mock.patch("apps.orchestrator.config_generator.generate_openclaw_config")
    @mock.patch("apps.orchestrator.services.update_tenant_config")
    def test_hibernated_location_change_defers(
        self, mock_update_cfg, mock_gen, mock_upload
    ):
        mock_gen.return_value = {"agent": {"name": "test"}}

        response = self.client.patch(
            "/api/v1/tenants/profile/",
            {"location_city": "Tokyo"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["applied"], "pending")

        mock_update_cfg.assert_not_called()
        self.assertTrue(mock_upload.called)
