"""Tests for ``apply_single_tenant_config_task`` hot-reload semantics.

The task writes the regenerated openclaw.json to the file share and then
asks the gateway to reload it. Azure Files (SMB) doesn't fire inotify
events, so without an explicit reload the running container keeps the
old manifest in memory until the next restart.

Phase 1 of the task swallowed every reload failure as a warning and
silently advanced ``config_version``, so when the gateway 502'd or the
revision was mid-restart the operator-visible state lied: ``config_version
== pending_config_version`` even though the file on disk was never read.
That class of silent failure is exactly the surface that hides plugin
toggles from a running session.

These tests pin the new contract:

* On reload success → advance ``config_version``, no follow-up.
* On transient reload failure → retry inline with bounded backoff.
* On persistent reload failure → DO NOT advance ``config_version``;
  queue a delayed re-apply so a future sweep gets another chance.
* For hibernated tenants → skip the reload (gateway is down) but still
  advance ``config_version`` because the file write is what wake reads.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.cron.gateway_client import GatewayError
from apps.orchestrator.tasks import apply_single_tenant_config_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class ApplySingleTenantConfigReloadTests(TestCase):
    """Hot-reload retry contract for ``apply_single_tenant_config_task``."""

    def setUp(self):
        self.tenant = create_tenant(display_name="ReloadRetry", telegram_chat_id=900300)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-reload-test"
        self.tenant.container_fqdn = "oc-reload-test.internal"
        self.tenant.config_version = 5
        self.tenant.pending_config_version = 6
        self.tenant.save()

    def test_reload_success_advances_config_version(self):
        with (
            patch("apps.orchestrator.tasks.update_tenant_config") as mock_update,
            patch("apps.orchestrator.tasks.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            mock_invoke.return_value = {}
            apply_single_tenant_config_task(str(self.tenant.id))

        mock_update.assert_called_once_with(str(self.tenant.id))
        mock_invoke.assert_called_once()
        # Reload was the only call.
        args, _ = mock_invoke.call_args
        self.assertEqual(args[1], "gateway.reload")

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 6)
        self.assertIsNotNone(self.tenant.config_refreshed_at)
        # No follow-up apply queued — reload succeeded.
        mock_publish.assert_not_called()

    def test_reload_retries_then_succeeds(self):
        attempts = []

        def reload_then_succeed(tenant, tool, args):
            attempts.append(tool)
            if len([t for t in attempts if t == "gateway.reload"]) < 3:
                raise GatewayError("Gateway returned 502")
            return {}

        with (
            patch("apps.orchestrator.tasks.update_tenant_config"),
            patch("apps.orchestrator.tasks.invoke_gateway_tool", side_effect=reload_then_succeed),
            patch("apps.orchestrator.tasks.time.sleep") as mock_sleep,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        # Three reload attempts, two backoffs slept between them.
        self.assertEqual(attempts.count("gateway.reload"), 3)
        self.assertEqual(mock_sleep.call_count, 2)

        # After eventual success, config_version advanced and no follow-up.
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 6)
        mock_publish.assert_not_called()

    def test_reload_persistent_failure_queues_followup_and_keeps_pending(self):
        with (
            patch("apps.orchestrator.tasks.update_tenant_config"),
            patch(
                "apps.orchestrator.tasks.invoke_gateway_tool",
                side_effect=GatewayError("Gateway returned 502"),
            ) as mock_invoke,
            patch("apps.orchestrator.tasks.time.sleep"),
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        # All bounded attempts exhausted.
        self.assertGreaterEqual(mock_invoke.call_count, 3)

        # config_version stays at 5: failed reload means config wasn't actually
        # applied to the running session, so the operator dashboard should keep
        # showing pending > current.
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 5)

        # A delayed re-apply was queued so a future sweep gets another shot.
        mock_publish.assert_called_once()
        args, kwargs = mock_publish.call_args
        self.assertEqual(args[0], "apply_single_tenant_config")
        self.assertEqual(args[1], str(self.tenant.id))
        # Delay must be long enough that we're not just hot-spinning.
        self.assertGreaterEqual(kwargs.get("delay_seconds", 0), 60)

    def test_reload_skipped_for_hibernated_tenant(self):
        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])

        with (
            patch("apps.orchestrator.tasks.update_tenant_config") as mock_update,
            patch("apps.orchestrator.tasks.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        # File still written — wake_hibernated_tenant relies on it being
        # current when the container comes back.
        mock_update.assert_called_once_with(str(self.tenant.id))
        # No reload attempt: the gateway is unreachable while hibernated, and
        # retrying would just churn the QStash queue with guaranteed-failed
        # POSTs.
        mock_invoke.assert_not_called()

        # config_version advances because the file is the source of truth at
        # wake time.
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 6)
        # No follow-up either — wake reads the file directly.
        mock_publish.assert_not_called()

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

    def test_followup_retry_does_not_recurse(self):
        # ``publish_task`` falls back to synchronous execution in dev (no
        # QStash token), so a naive follow-up enqueue would re-enter this
        # same task and infinite-loop. The ``_is_followup_retry`` guard
        # caps the recursion at one extra cycle.
        with (
            patch("apps.orchestrator.tasks.update_tenant_config"),
            patch(
                "apps.orchestrator.tasks.invoke_gateway_tool",
                side_effect=GatewayError("Gateway returned 502"),
            ) as mock_invoke,
            patch("apps.orchestrator.tasks.time.sleep"),
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            apply_single_tenant_config_task(str(self.tenant.id), _is_followup_retry=True)

        # Reload retries are still attempted on the follow-up...
        self.assertGreaterEqual(mock_invoke.call_count, 3)
        # ...but no further apply is enqueued (would recurse in sync mode).
        mock_publish.assert_not_called()

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 5)

    def test_update_tenant_config_failure_does_not_advance(self):
        # If the file write itself fails, neither version nor reload move.
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
        # No follow-up here either — caller (apply-pending-configs sweep) will
        # retry naturally on its next tick.
        mock_publish.assert_not_called()
