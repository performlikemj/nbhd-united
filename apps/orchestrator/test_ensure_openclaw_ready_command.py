"""ensure_openclaw_ready command flow (SimpleTestCase — Tenant lookup, the
Azure reconcile, the cron guard and the image task are all patched).

Covers the review-round guarantees: writes defer behind the cron guard,
dry-run never probes the gateway or writes, an allowlisted roll is ONE
restart (no separate storage write), a gated tenant gets storage only, and
``--tenant`` refuses a hibernated tenant.
"""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from apps.orchestrator.azure_client import StorageReconcileResult
from apps.orchestrator.openclaw_drift import FIELD_OC_STATE_VOLUME, FieldDrift
from apps.orchestrator.test_openclaw_drift import _converged_app

CANARY = "148ccf1c-ef13-47f8-aaaa-bbbbbbbbbbbb"
CMD = "apps.orchestrator.management.commands.ensure_openclaw_ready"


def _tenant(hibernated=False):
    return SimpleNamespace(
        id=CANARY,
        container_id="oc-148ccf1c-ef13-47f8-a",
        container_image_tag="2026.9.4-abc1234",
        hibernated_at="2026-09-01T00:00:00Z" if hibernated else None,
    )


def _drifted(container: str, dry_run: bool) -> StorageReconcileResult:
    before = [FieldDrift(FIELD_OC_STATE_VOLUME, "EmptyDir", "missing")]
    return StorageReconcileResult(container_name=container, drift_before=before, dry_run=dry_run)


def _in_sync(container: str, dry_run: bool) -> StorageReconcileResult:
    return StorageReconcileResult(container_name=container, dry_run=dry_run)


@override_settings(AZURE_RESOURCE_GROUP="rg-test", OPENCLAW_IMAGE_TAG="2026.9.5-def5678")
@patch("apps.orchestrator.azure_client._is_mock", return_value=False)
@patch("apps.orchestrator.hibernation._cron_active_or_imminent", return_value=None)
@patch("apps.orchestrator.tasks.apply_single_tenant_image_task")
@patch(f"{CMD}.get_container_client", create=True)
@patch("apps.orchestrator.azure_client.get_container_client")
@patch(f"{CMD}.ensure_openclaw_storage_ready")
@patch(f"{CMD}.Command._tenants")
class EnsureOpenclawReadyCommandTest(SimpleTestCase):
    def _run(self, *args, storage, tenants_mock, ensure_mock, client_mock, **kw):
        tenants_mock.return_value = [_tenant()]
        ensure_mock.side_effect = lambda c, dry_run=False: storage(c, dry_run)
        client = MagicMock()
        client.container_apps.get.return_value = _converged_app()  # live image = 2026.9.4-abc1234
        client_mock.return_value = client
        out = StringIO()
        call_command("ensure_openclaw_ready", "--all", *args, stdout=out, stderr=out, **kw)
        return out.getvalue()

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS="")
    def test_gated_tenant_gets_storage_write_only(self, tenants, ensure, client, _cc, task, guard, _mock):
        out = self._run(storage=_drifted, tenants_mock=tenants, ensure_mock=ensure, client_mock=client)
        # probe (dry_run=True) then the real write
        self.assertEqual([c.kwargs.get("dry_run", False) for c in ensure.call_args_list], [True, False])
        guard.assert_called_once()
        task.assert_not_called()
        self.assertIn("stale-gated", out)
        self.assertIn("1 fixed", out)

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=CANARY)
    def test_allowlisted_roll_is_one_restart(self, tenants, ensure, client, _cc, task, guard, _mock):
        with patch(f"{CMD}.Tenant") as tenant_model:
            tenant_model.objects.filter.return_value.values_list.return_value.first.return_value = "2026.9.5-def5678"
            out = self._run(storage=_drifted, tenants_mock=tenants, ensure_mock=ensure, client_mock=client)
        # Only the read-only probe — the image bump bakes the storage state in.
        self.assertEqual([c.kwargs.get("dry_run", False) for c in ensure.call_args_list], [True])
        task.assert_called_once_with(CANARY, "2026.9.5-def5678")
        self.assertIn("ROLLED", out)
        self.assertIn("1 fixed", out)

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=CANARY)
    def test_failed_roll_is_counted(self, tenants, ensure, client, _cc, task, guard, _mock):
        with patch(f"{CMD}.Tenant") as tenant_model:
            tenant_model.objects.filter.return_value.values_list.return_value.first.return_value = "2026.9.4-abc1234"
            out = self._run(storage=_in_sync, tenants_mock=tenants, ensure_mock=ensure, client_mock=client)
        self.assertIn("roll FAILED", out)
        self.assertIn("1 failed", out)

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=CANARY)
    def test_cron_guard_defers_every_write(self, tenants, ensure, client, _cc, task, guard, _mock):
        guard.return_value = "cron_imminent"
        out = self._run(storage=_drifted, tenants_mock=tenants, ensure_mock=ensure, client_mock=client)
        self.assertEqual([c.kwargs.get("dry_run", False) for c in ensure.call_args_list], [True])
        task.assert_not_called()
        self.assertIn("DEFERRED (cron_imminent)", out)
        self.assertIn("1 deferred", out)

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=CANARY)
    def test_dry_run_never_probes_gateway_or_writes(self, tenants, ensure, client, _cc, task, guard, _mock):
        out = self._run("--dry-run", storage=_drifted, tenants_mock=tenants, ensure_mock=ensure, client_mock=client)
        self.assertEqual([c.kwargs.get("dry_run", False) for c in ensure.call_args_list], [True])
        guard.assert_not_called()
        task.assert_not_called()
        self.assertIn("would fix 1", out)
        self.assertIn("1 to fix", out)

    @override_settings(OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=CANARY)
    def test_no_image_skips_roll_but_fixes_storage(self, tenants, ensure, client, _cc, task, guard, _mock):
        out = self._run("--no-image", storage=_drifted, tenants_mock=tenants, ensure_mock=ensure, client_mock=client)
        self.assertEqual([c.kwargs.get("dry_run", False) for c in ensure.call_args_list], [True, False])
        task.assert_not_called()
        self.assertIn("skipped (--no-image)", out)

    def test_converged_tenant_is_a_noop(self, tenants, ensure, client, _cc, task, guard, _mock):
        with override_settings(OPENCLAW_IMAGE_TAG="2026.9.4-abc1234"):
            out = self._run(storage=_in_sync, tenants_mock=tenants, ensure_mock=ensure, client_mock=client)
        self.assertEqual([c.kwargs.get("dry_run", False) for c in ensure.call_args_list], [True])
        guard.assert_not_called()
        self.assertIn("1 in sync", out)
        self.assertIn("image: ok", out)


@patch("apps.orchestrator.azure_client._is_mock", return_value=False)
class EnsureOpenclawReadyTargetingTest(SimpleTestCase):
    def test_single_hibernated_tenant_is_refused(self, _mock):
        with patch(f"{CMD}.Tenant") as tenant_model:
            tenant_model.objects.filter.return_value.first.return_value = _tenant(hibernated=True)
            with self.assertRaises(CommandError) as ctx:
                call_command("ensure_openclaw_ready", "--tenant", CANARY, stdout=StringIO())
        self.assertIn("hibernated", str(ctx.exception))

    def test_mock_short_circuits_before_db(self, mock_is_mock):
        mock_is_mock.return_value = True
        out = StringIO()
        with patch(f"{CMD}.Command._tenants") as tenants:
            call_command("ensure_openclaw_ready", "--all", stdout=out)
        tenants.assert_not_called()
        self.assertIn("[MOCK]", out.getvalue())
