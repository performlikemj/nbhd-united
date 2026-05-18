"""Tests for ``apply_single_tenant_config_task`` apply semantics.

The task writes the regenerated openclaw.json to the file share and then
advances ``config_version`` / ``applied_model``. The file write IS the
apply: OpenClaw 2026.5.7's policy pipeline blocks the ``gateway`` tool
from the HTTP ``/tools/invoke`` registry (the same pipeline runs for
agent context and HTTP), and the legacy ``gateway.reload`` action does
not exist on 2026.5.7. Pickup of the new config happens on the next
container restart — see issue #541 for the full reload-shadowing trace.

These tests pin the new contract:

* Successful file write → advance ``config_version``, stamp
  ``applied_model``. No gateway call.
* File-write failure → leave ``config_version`` behind. No gateway call.
* Hibernated tenant → advance ``config_version`` the same way the
  active path does (``wake_hibernated_tenant`` reads the file at wake).
* No-op if ``config_version`` is already current.
* ``_is_followup_retry`` is accepted but doesn't change behavior (legacy
  QStash deliveries from the old reload-loop code path must still drain).
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.orchestrator.tasks import apply_single_tenant_config_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class ApplySingleTenantConfigTests(TestCase):
    """Apply contract for ``apply_single_tenant_config_task``."""

    def setUp(self):
        self.tenant = create_tenant(display_name="ReloadRetry", telegram_chat_id=900300)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-reload-test"
        self.tenant.container_fqdn = "oc-reload-test.internal"
        self.tenant.config_version = 5
        self.tenant.pending_config_version = 6
        self.tenant.save()

    def test_file_write_advances_config_version_without_gateway_call(self):
        with (
            patch("apps.orchestrator.tasks.update_tenant_config") as mock_update,
            patch("apps.orchestrator.tasks.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        mock_update.assert_called_once_with(str(self.tenant.id))
        # No live reload call: the file write is the apply.
        mock_invoke.assert_not_called()
        mock_publish.assert_not_called()

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 6)
        self.assertIsNotNone(self.tenant.config_refreshed_at)

    def test_hibernated_tenant_advances_same_as_active(self):
        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])

        with (
            patch("apps.orchestrator.tasks.update_tenant_config") as mock_update,
            patch("apps.orchestrator.tasks.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        mock_update.assert_called_once_with(str(self.tenant.id))
        mock_invoke.assert_not_called()
        mock_publish.assert_not_called()

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 6)

    def test_no_op_when_config_already_current(self):
        self.tenant.config_version = 6
        self.tenant.save(update_fields=["config_version"])

        with (
            patch("apps.orchestrator.tasks.update_tenant_config") as mock_update,
            patch("apps.orchestrator.tasks.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        mock_update.assert_not_called()
        mock_invoke.assert_not_called()
        mock_publish.assert_not_called()

    def test_legacy_followup_retry_arg_is_accepted(self):
        # In-flight QStash deliveries enqueued by the previous reload-loop
        # code path still pass ``_is_followup_retry=True``. They must drain
        # without raising — same behavior as the no-arg path now.
        with (
            patch("apps.orchestrator.tasks.update_tenant_config") as mock_update,
            patch("apps.orchestrator.tasks.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_config_task(str(self.tenant.id), _is_followup_retry=True)

        mock_update.assert_called_once_with(str(self.tenant.id))
        mock_invoke.assert_not_called()
        mock_publish.assert_not_called()

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 6)

    def test_update_tenant_config_failure_does_not_advance(self):
        with (
            patch(
                "apps.orchestrator.tasks.update_tenant_config",
                side_effect=RuntimeError("file share unreachable"),
            ),
            patch("apps.orchestrator.tasks.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 5)
        mock_invoke.assert_not_called()
        mock_publish.assert_not_called()
