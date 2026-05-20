"""Coverage for ``refresh_user_md_fleet_task``.

Pins the staleness-bound semantics:
  * Active tenants with a container_id get a forced USER.md push.
  * Tenants without a container_id (provisioning, deleted, etc.) are skipped.
  * Per-tenant failures don't abort the fleet sweep.
  * The push uses ``force=True`` + ``debounce_seconds=0`` so the hourly
    sweep is never collapsed by the 60s leading-edge debounce.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.tasks import refresh_user_md_fleet_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class RefreshUserMdFleetTaskTests(TestCase):
    def _make_active_tenant(self, *, suffix: int, container_id: str = "") -> Tenant:
        tenant = create_tenant(
            display_name=f"Fleet Refresh {suffix}",
            telegram_chat_id=950_000_000 + suffix,
        )
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = container_id or f"oc-fleet-{suffix}"
        tenant.container_fqdn = f"oc-fleet-{suffix}.internal"
        tenant.save()
        return tenant

    @patch("apps.orchestrator.workspace_envelope.push_user_md", return_value=True)
    def test_pushes_for_every_active_tenant_with_a_container(self, mock_push):
        t1 = self._make_active_tenant(suffix=1)
        t2 = self._make_active_tenant(suffix=2)

        result = refresh_user_md_fleet_task()

        self.assertEqual(result["pushed"], 2)
        self.assertEqual(result["failed"], 0)
        pushed_ids = {call.args[0].id for call in mock_push.call_args_list}
        self.assertEqual(pushed_ids, {t1.id, t2.id})

    @patch("apps.orchestrator.workspace_envelope.push_user_md", return_value=True)
    def test_uses_force_and_zero_debounce(self, mock_push):
        """Hourly sweep must bypass the leading-edge debounce — otherwise
        a recent organic refresh would suppress the staleness backstop.
        """
        self._make_active_tenant(suffix=3)

        refresh_user_md_fleet_task()

        kwargs = mock_push.call_args.kwargs
        self.assertTrue(kwargs.get("force"))
        self.assertEqual(kwargs.get("debounce_seconds"), 0)

    @patch("apps.orchestrator.workspace_envelope.push_user_md", return_value=True)
    def test_skips_tenants_without_a_container(self, mock_push):
        """A tenant mid-provisioning has no file share to write to —
        ``container_id=""`` is the exclusion signal mirroring
        ``refresh_user_md`` management command.
        """
        active = self._make_active_tenant(suffix=4)
        pending = self._make_active_tenant(suffix=5, container_id="")
        pending.container_id = ""
        pending.save(update_fields=["container_id"])

        result = refresh_user_md_fleet_task()

        self.assertEqual(result["pushed"], 1)
        pushed_ids = {call.args[0].id for call in mock_push.call_args_list}
        self.assertEqual(pushed_ids, {active.id})

    @patch("apps.orchestrator.workspace_envelope.push_user_md")
    def test_one_tenant_failure_does_not_abort_the_fleet(self, mock_push):
        t1 = self._make_active_tenant(suffix=6)
        t2 = self._make_active_tenant(suffix=7)

        def _raise_for_first(tenant, *args, **kwargs):
            if tenant.id == t1.id:
                raise RuntimeError("simulated share write failure")
            return True

        mock_push.side_effect = _raise_for_first

        result = refresh_user_md_fleet_task()

        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["failed"], 1)
        # Both tenants were attempted — failure didn't short-circuit.
        attempted_ids = {call.args[0].id for call in mock_push.call_args_list}
        self.assertEqual(attempted_ids, {t1.id, t2.id})
