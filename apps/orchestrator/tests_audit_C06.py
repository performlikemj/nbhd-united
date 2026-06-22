"""Regression coverage for fix cluster C06 (feature audit).

Covers three confirmed defects:

* FA-0819 — the Postgres-canonical tenant cron reconciler
  (``regenerate_tenant_crons``) must NOT own or remove live ``_fuel:*``
  per-session crons. Those jobs are written straight to the gateway by
  ``regenerate_fuel_crons`` with no backing ``managed=True`` CronJob row, so
  before the fix the tenant reconciler classified them as stale-and-managed
  and removed them on every pass — a destructive flapping race.

* FA-0824 / FA-0866 — ``regenerate_fuel_crons`` (and therefore the hourly
  fleet sweep ``reconcile_fuel_crons_task``) must skip hibernated/suspended
  tenants instead of POSTing ``cron.list`` to a scaled-to-zero container,
  which raised GatewayError + inflated error telemetry every hour.

* FA-0846 — ``apply_single_tenant_config_task`` must not advance
  ``config_version`` when ``update_tenant_config`` silently skipped the
  file-share write because the tenant is no longer ACTIVE / lost its
  container. Advancing it would falsely mark the change applied and strand
  it past every retry path.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.orchestrator.cron_reconcile import regenerate_tenant_crons
from apps.orchestrator.fuel_cron import regenerate_fuel_crons
from apps.orchestrator.tasks import apply_single_tenant_config_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _fuel_gateway_job(short_hex: str = "deadbeef") -> dict:
    """A live per-session fuel cron as it appears in a ``cron.list`` result."""
    return {
        "id": f"gw-{short_hex}",
        "name": f"_fuel:{short_hex}",
        "schedule": {"kind": "cron", "expr": "30 7 9 6 *", "tz": "Asia/Tokyo"},
        "createdAtMs": 1,
    }


class FuelCronNotReapedByTenantReconcilerTests(TestCase):
    """FA-0819: tenant reconciler leaves live ``_fuel:*`` crons untouched."""

    def setUp(self):
        self.tenant = create_tenant(display_name="FuelReconcile", telegram_chat_id=818181)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-test"
        self.tenant.container_fqdn = "oc-test.internal"
        self.tenant.postgres_cron_canonical = True
        self.tenant.save()

    def test_fuel_session_cron_survives_tenant_reconcile(self):
        fuel_job = _fuel_gateway_job()

        def _fake_invoke(tenant, tool, payload):
            if tool == "cron.list":
                return {"jobs": [fuel_job]}
            return {}

        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            side_effect=_fake_invoke,
        ) as mock_invoke:
            summary = regenerate_tenant_crons(self.tenant)

        # The fuel job must never be handed to cron.remove.
        removed_ids = [
            call.args[2].get("jobId") for call in mock_invoke.call_args_list if call.args[1] == "cron.remove"
        ]
        self.assertNotIn(fuel_job["id"], removed_ids)
        self.assertEqual(summary["removed"], 0)
        self.assertEqual(summary["duplicates_reaped"], 0)


class RegenerateFuelCronsHibernationGuardTests(TestCase):
    """FA-0824 / FA-0866: skip hibernated/suspended tenants, no gateway call."""

    def setUp(self):
        from apps.fuel.models import FuelProfile

        self.tenant = create_tenant(display_name="FuelHibernate", telegram_chat_id=919191)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-test"
        self.tenant.container_fqdn = "oc-test.internal"
        self.tenant.save()
        FuelProfile.objects.create(tenant=self.tenant, use_session_scheduling=True)

    def test_skips_hibernated_tenant_without_gateway_call(self):
        self.tenant.hibernated_at = timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])
        self.tenant.refresh_from_db()

        with patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_invoke:
            summary = regenerate_fuel_crons(self.tenant)

        mock_invoke.assert_not_called()
        self.assertEqual(summary["errors"], 0)

    def test_skips_suspended_tenant_without_gateway_call(self):
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.save(update_fields=["status"])
        self.tenant.refresh_from_db()

        with patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_invoke:
            summary = regenerate_fuel_crons(self.tenant)

        mock_invoke.assert_not_called()
        self.assertEqual(summary["errors"], 0)


class ApplyConfigSkippedWriteVersionGuardTests(TestCase):
    """FA-0846: do not advance config_version when the write was skipped."""

    def setUp(self):
        self.tenant = create_tenant(display_name="ApplySkip", telegram_chat_id=727272)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-test"
        self.tenant.container_fqdn = "oc-test.internal"
        self.tenant.config_version = 1
        self.tenant.pending_config_version = 2
        self.tenant.save()

    def test_does_not_advance_version_when_tenant_suspended_mid_apply(self):
        # update_tenant_config (mocked) silently no-ops when the tenant is no
        # longer ACTIVE; simulate the tenant flipping to SUSPENDED during the
        # apply so the file-share write never happened.
        def _flip_suspended(tenant_id):
            Tenant.objects.filter(id=tenant_id).update(status=Tenant.Status.SUSPENDED)

        with (
            patch(
                "apps.orchestrator.tasks.update_tenant_config",
                side_effect=_flip_suspended,
            ),
            patch("apps.cron.publish.publish_task"),
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        self.tenant.refresh_from_db()
        # Version must stay behind pending so the natural re-queue paths retry.
        self.assertEqual(self.tenant.config_version, 1)
        self.assertEqual(self.tenant.pending_config_version, 2)

    def test_advances_version_for_active_tenant(self):
        with (
            patch("apps.orchestrator.tasks.update_tenant_config"),
            patch("apps.cron.publish.publish_task"),
        ):
            apply_single_tenant_config_task(str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.config_version, 2)
