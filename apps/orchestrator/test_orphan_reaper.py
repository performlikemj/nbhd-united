"""Tests for the orphaned-container reaper + the tenant-delete hibernation hook."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.orchestrator import orphan_reaper
from apps.tenants.models import Tenant

AZ = "apps.orchestrator.azure_client"


def _make_tenant(*, tenant_id: uuid.UUID | None = None, container_id: str = "") -> Tenant:
    User = get_user_model()
    user = User.objects.create_user(
        username=f"u-{uuid.uuid4()}",
        email=f"u-{uuid.uuid4()}@example.com",
    )
    return Tenant.objects.create(
        id=tenant_id or uuid.uuid4(),
        user=user,
        container_id=container_id,
    )


class FindOrphanedContainersTest(TestCase):
    def test_identifies_only_containers_with_no_tenant(self):
        # A tenant owned via container_id, another owned via the oc-<id[:20]>
        # convention, and two true orphans.
        owned_by_field = _make_tenant(container_id="oc-field-owned-xyz")
        tid = uuid.UUID("44aaee8d-4b03-4da3-9747-166a6fbcef3c")
        _make_tenant(tenant_id=tid)  # container_id empty -> matched by id prefix
        live = [
            "oc-field-owned-xyz",  # owned by container_id
            "oc-44aaee8d-4b03-4da3-9",  # owned by id-prefix (str(tid)[:20])
            "oc-873cf419-e3ef-4d95-a",  # orphan
            "oc-deadbeef-0000-0000-0",  # orphan
        ]
        assert owned_by_field.container_id == "oc-field-owned-xyz"
        with patch(f"{AZ}.list_tenant_container_app_names", return_value=live):
            orphans = orphan_reaper.find_orphaned_container_names()
        self.assertEqual(
            sorted(orphans),
            ["oc-873cf419-e3ef-4d95-a", "oc-deadbeef-0000-0000-0"],
        )

    def test_no_live_containers_returns_empty(self):
        with patch(f"{AZ}.list_tenant_container_app_names", return_value=[]):
            self.assertEqual(orphan_reaper.find_orphaned_container_names(), [])


class ReapOrphanedContainersTest(TestCase):
    def setUp(self):
        # One orphan in the fleet for these tests.
        self.orphan = "oc-873cf419-e3ef-4d95-a"
        self._find = patch(
            f"{AZ}.list_tenant_container_app_names",
            return_value=[self.orphan],
        )
        self._find.start()
        self.addCleanup(self._find.stop)

    def test_awake_orphan_is_hibernated_and_alerts(self):
        with (
            patch(f"{AZ}.container_app_has_active_revision", return_value=True),
            patch(f"{AZ}.hibernate_container_app") as hib,
            patch("apps.cron.views._send_alert_to_personal_openclaw", return_value="delivered") as alert,
        ):
            summary = orphan_reaper.reap_orphaned_containers(hibernate=True, apply=False, alert=True)

        hib.assert_called_once_with(self.orphan)
        alert.assert_called_once()
        self.assertEqual(summary["orphans"], [self.orphan])
        self.assertEqual(summary["awake"], [self.orphan])
        self.assertEqual(summary["hibernated"], [self.orphan])
        self.assertEqual(summary["torn_down"], {})

    def test_dormant_orphan_is_not_hibernated(self):
        with (
            patch(f"{AZ}.container_app_has_active_revision", return_value=False),
            patch(f"{AZ}.hibernate_container_app") as hib,
            patch("apps.cron.views._send_alert_to_personal_openclaw", return_value="delivered"),
        ):
            summary = orphan_reaper.reap_orphaned_containers(hibernate=True, apply=False, alert=True)

        hib.assert_not_called()
        self.assertEqual(summary["awake"], [])
        self.assertEqual(summary["hibernated"], [])

    def test_dry_run_does_not_hibernate(self):
        with (
            patch(f"{AZ}.container_app_has_active_revision", return_value=True),
            patch(f"{AZ}.hibernate_container_app") as hib,
        ):
            summary = orphan_reaper.reap_orphaned_containers(hibernate=False, apply=False, alert=False)

        hib.assert_not_called()
        self.assertEqual(summary["awake"], [self.orphan])
        self.assertEqual(summary["hibernated"], [])

    def test_apply_attempts_full_teardown(self):
        with (
            patch(f"{AZ}.container_app_has_active_revision", return_value=False),
            patch(f"{AZ}.delete_container_app") as del_app,
            patch(f"{AZ}.delete_tenant_file_share") as del_share,
            patch(f"{AZ}.delete_managed_identity") as del_mi,
            patch("apps.cron.views._send_alert_to_personal_openclaw", return_value="delivered"),
        ):
            summary = orphan_reaper.reap_orphaned_containers(hibernate=True, apply=True, alert=True)

        del_app.assert_called_once_with(self.orphan)
        # file-share / managed-identity called with the oc- prefix stripped
        del_share.assert_called_once_with("873cf419-e3ef-4d95-a")
        del_mi.assert_called_once_with("873cf419-e3ef-4d95-a")
        self.assertEqual(summary["torn_down"][self.orphan]["container"], "deleted")

    def test_apply_classifies_lock_block(self):
        lock_exc = Exception(
            "(ScopeLocked) The scope '...' cannot perform delete operation because following scope(s) are locked"
        )
        with (
            patch(f"{AZ}.container_app_has_active_revision", return_value=False),
            patch(f"{AZ}.delete_container_app", side_effect=lock_exc),
            patch(f"{AZ}.delete_tenant_file_share"),
            patch(f"{AZ}.delete_managed_identity"),
            patch("apps.cron.views._send_alert_to_personal_openclaw", return_value="delivered"),
        ):
            summary = orphan_reaper.reap_orphaned_containers(hibernate=False, apply=True, alert=False)

        self.assertEqual(summary["torn_down"][self.orphan]["container"], "blocked")

    def test_no_orphans_no_alert(self):
        self._find.stop()
        with (
            patch(f"{AZ}.list_tenant_container_app_names", return_value=["oc-known"]),
            patch("apps.cron.views._send_alert_to_personal_openclaw") as alert,
        ):
            _make_tenant(container_id="oc-known")
            summary = orphan_reaper.reap_orphaned_containers()
        self.assertEqual(summary["orphans"], [])
        alert.assert_not_called()
        self._find.start()  # so addCleanup.stop() doesn't error


class TenantDeleteHibernationSignalTest(TestCase):
    def test_deleting_tenant_hibernates_its_container(self):
        tenant = _make_tenant(container_id="oc-signal-test-1")
        with patch(f"{AZ}.hibernate_container_app") as hib:
            tenant.delete()
        hib.assert_called_once_with("oc-signal-test-1")

    def test_user_cascade_delete_hibernates_container(self):
        """The real orphan path: deleting the User cascade-deletes the Tenant."""
        tenant = _make_tenant(container_id="oc-cascade-1")
        user = tenant.user
        with patch(f"{AZ}.hibernate_container_app") as hib:
            user.delete()
        hib.assert_called_once_with("oc-cascade-1")

    def test_no_container_id_is_noop(self):
        tenant = _make_tenant(container_id="")
        with patch(f"{AZ}.hibernate_container_app") as hib:
            tenant.delete()
        hib.assert_not_called()

    def test_hibernate_failure_does_not_block_delete(self):
        tenant = _make_tenant(container_id="oc-flaky-1")
        tid = tenant.id
        with patch(f"{AZ}.hibernate_container_app", side_effect=RuntimeError("azure down")):
            tenant.delete()  # must not raise
        self.assertFalse(Tenant.objects.filter(id=tid).exists())
