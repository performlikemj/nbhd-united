"""Coverage for ``applied_model`` stamping in apply_single_tenant_config_task.

The dashboard reads ``applied_model`` vs ``preferred_model`` to render an
honest "Switching…" badge while a picker change is in flight. The stamp
must follow ``config_version`` exactly: only land when the running
container has been told (or — for hibernated tenants — will be told at
wake) about the new config.

The retry / follow-up / no-op machinery itself is covered in
``test_apply_config_reload.py``; here we just pin the picker invariant.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.cron.gateway_client import GatewayError
from apps.orchestrator.tasks import apply_single_tenant_config_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

OLD_MODEL = "openrouter/google/gemma-4-31b-it"
NEW_MODEL = "openrouter/minimax/minimax-m2.7"


class ApplySingleTenantConfigAppliedModelTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="AppliedModel", telegram_chat_id=717171)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-test"
        self.tenant.container_fqdn = "oc-test.internal"
        self.tenant.preferred_model = NEW_MODEL
        self.tenant.applied_model = OLD_MODEL
        self.tenant.config_version = 1
        self.tenant.pending_config_version = 2
        self.tenant.save()

    def test_stamps_applied_model_after_successful_reload(self):
        with (
            patch("apps.orchestrator.tasks.update_tenant_config"),
            patch("apps.orchestrator.tasks.invoke_gateway_tool", return_value={}),
            patch("apps.cron.publish.publish_task"),
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.applied_model, NEW_MODEL)
        self.assertIsNotNone(self.tenant.applied_model_at)
        self.assertEqual(self.tenant.config_version, 2)

    def test_does_not_stamp_when_reload_fails(self):
        with (
            patch("apps.orchestrator.tasks.update_tenant_config"),
            patch(
                "apps.orchestrator.tasks.invoke_gateway_tool",
                side_effect=GatewayError("502"),
            ),
            patch("apps.orchestrator.tasks.time.sleep"),
            patch("apps.cron.publish.publish_task"),
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        self.tenant.refresh_from_db()
        # The dashboard should still read 'Switching…' because the running
        # container hasn't actually adopted the new model.
        self.assertEqual(self.tenant.applied_model, OLD_MODEL)
        self.assertIsNone(self.tenant.applied_model_at)
        self.assertEqual(self.tenant.config_version, 1)

    def test_stamps_applied_model_for_hibernated_tenant(self):
        # Hibernated path: gateway is unreachable, but wake reads the file
        # directly. The file write IS the apply, so applied_model stamps
        # along with config_version.
        from django.utils import timezone

        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])

        with (
            patch("apps.orchestrator.tasks.update_tenant_config"),
            patch("apps.orchestrator.tasks.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.publish.publish_task"),
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        mock_invoke.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.applied_model, NEW_MODEL)
        self.assertIsNotNone(self.tenant.applied_model_at)
        self.assertEqual(self.tenant.config_version, 2)

    def test_does_not_stamp_when_file_write_fails(self):
        with (
            patch(
                "apps.orchestrator.tasks.update_tenant_config",
                side_effect=RuntimeError("file share unreachable"),
            ),
            patch("apps.orchestrator.tasks.invoke_gateway_tool"),
            patch("apps.cron.publish.publish_task"),
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.applied_model, OLD_MODEL)
        self.assertIsNone(self.tenant.applied_model_at)
        self.assertEqual(self.tenant.config_version, 1)
