"""Regression coverage for adversarial audit cluster A39.

Covers one confirmed defect:

* orchestrator-provisioning#3 — ``repair_stale_tenant_provisioning`` includes
  ACTIVE tenants with empty container fields in its queryset (the repair sweep
  is explicitly designed to fix them), but ``provision_tenant`` short-circuits
  on any status that is not PENDING or PROVISIONING, so ACTIVE+empty tenants
  were silently counted as ``failed`` on every run with no repair path.

  Fix: in ``repair_stale_tenant_provisioning``, demote an ACTIVE tenant with
  empty container fields to PROVISIONING before calling ``provision_tenant``,
  keeping ``provision_tenant``'s own invariant intact.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.services import repair_stale_tenant_provisioning
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

# Minimal fake result returned by the mocked provision_tenant helper so the
# repair path can set container_id / container_fqdn and flip to ACTIVE.
_FAKE_PROVISION_RESULT = {
    "name": "oc-test-repaired",
    "fqdn": "oc-test-repaired.internal",
}


def _mock_provision_tenant(tenant_id: str) -> None:
    """Side-effect: writes container fields + sets status=ACTIVE on the DB row,
    mimicking what the real provision_tenant does at step 4."""
    tenant = Tenant.objects.get(id=tenant_id)
    tenant.container_id = _FAKE_PROVISION_RESULT["name"]
    tenant.container_fqdn = _FAKE_PROVISION_RESULT["fqdn"]
    tenant.status = Tenant.Status.ACTIVE
    tenant.save(update_fields=["container_id", "container_fqdn", "status", "updated_at"])


class ActiveTenantEmptyContainerRepairTests(TestCase):
    """orchestrator-provisioning#3: ACTIVE+empty-container tenants are repaired."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Repair Test",
            telegram_chat_id=39393939,
        )
        # Put the tenant into the ACTIVE+empty-fields corrupted state.
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = ""
        self.tenant.container_fqdn = ""
        self.tenant.save()

    def test_active_empty_tenant_is_in_sweep_queryset(self):
        """The repair queryset must include ACTIVE tenants with empty fields."""
        from apps.orchestrator.services import _stale_provisioning_tenants_queryset

        qs = _stale_provisioning_tenants_queryset()
        self.assertIn(self.tenant, list(qs))

    def test_repair_demotes_active_to_provisioning_before_calling_provision(self):
        """Before calling provision_tenant, the repair loop demotes ACTIVE→PROVISIONING.

        This is verified by inspecting the tenant's status as seen by
        provision_tenant (which accepts PENDING/PROVISIONING only).
        """
        captured_status = {}

        def _capturing_provision(tenant_id: str) -> None:
            t = Tenant.objects.get(id=tenant_id)
            captured_status["status_at_entry"] = t.status
            _mock_provision_tenant(tenant_id)

        with patch(
            "apps.orchestrator.services.provision_tenant",
            side_effect=_capturing_provision,
        ):
            repair_stale_tenant_provisioning(tenant_id=str(self.tenant.id))

        self.assertEqual(
            captured_status.get("status_at_entry"),
            Tenant.Status.PROVISIONING,
            "provision_tenant should see PROVISIONING, not ACTIVE, so its status guard accepts the call",
        )

    def test_repair_counts_active_empty_tenant_as_repaired(self):
        """An ACTIVE+empty tenant that re-provisions successfully counts as repaired, not failed."""
        with patch(
            "apps.orchestrator.services.provision_tenant",
            side_effect=_mock_provision_tenant,
        ):
            summary = repair_stale_tenant_provisioning(tenant_id=str(self.tenant.id))

        self.assertEqual(summary["repaired"], 1, "Should be counted as repaired")
        self.assertEqual(summary["failed"], 0, "Should not be counted as failed")
        self.assertEqual(summary["evaluated"], 1)

    def test_repair_leaves_tenant_active_with_container_fields(self):
        """After a successful repair, the tenant is ACTIVE with both container fields set."""
        with patch(
            "apps.orchestrator.services.provision_tenant",
            side_effect=_mock_provision_tenant,
        ):
            repair_stale_tenant_provisioning(tenant_id=str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, Tenant.Status.ACTIVE)
        self.assertEqual(self.tenant.container_id, _FAKE_PROVISION_RESULT["name"])
        self.assertEqual(self.tenant.container_fqdn, _FAKE_PROVISION_RESULT["fqdn"])

    def test_pending_and_provisioning_tenants_unaffected(self):
        """The demotion logic must NOT touch PENDING or PROVISIONING tenants."""
        pending_tenant = create_tenant(display_name="Pending Test", telegram_chat_id=39393940)
        # PENDING is the default from create_tenant; clear container fields explicitly.
        pending_tenant.container_id = ""
        pending_tenant.container_fqdn = ""
        pending_tenant.save()

        captured = {}

        def _capture_status(tenant_id: str) -> None:
            t = Tenant.objects.get(id=tenant_id)
            captured[tenant_id] = t.status
            _mock_provision_tenant(tenant_id)

        with patch(
            "apps.orchestrator.services.provision_tenant",
            side_effect=_capture_status,
        ):
            repair_stale_tenant_provisioning(tenant_id=str(pending_tenant.id))

        self.assertEqual(
            captured.get(str(pending_tenant.id)),
            Tenant.Status.PENDING,
            "PENDING tenants should enter provision_tenant as PENDING (no demotion needed)",
        )
