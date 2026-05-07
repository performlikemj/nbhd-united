"""Tests for migration ``tenants.0051_byo_models_enabled_default_true``.

Phase 1 BYO Anthropic shipped with ``byo_models_enabled`` defaulting to
False and a per-tenant canary flag flip. The fleet rollout migration
flips every non-deleted tenant to True and changes the model default,
so newly provisioned tenants are auto-enabled.

These tests exercise the data-migration function directly (not via
``call_command('migrate')``, which is heavy and brittle in CI). The
schema-default change is covered by the fact that ``create_tenant`` now
returns rows with ``byo_models_enabled=True`` after the migration ships.
"""

from __future__ import annotations

from django.test import TestCase

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _set_flag(tenant: Tenant, value: bool, status: str | None = None) -> None:
    """Flip the flag (and optionally status) under the hood, bypassing the
    new model default — so we can simulate the pre-migration state."""
    fields = ["byo_models_enabled"]
    Tenant.objects.filter(id=tenant.id).update(byo_models_enabled=value)
    if status is not None:
        Tenant.objects.filter(id=tenant.id).update(status=status)
        fields.append("status")
    tenant.refresh_from_db(fields=fields)


class ByoFleetMigrationTest(TestCase):
    """The migration's ``RunPython`` forward function flips every
    non-deleted tenant to ``byo_models_enabled=True``."""

    def setUp(self):
        # `create_tenant` factory uses model defaults — after this PR the
        # default is True. Force each row to False below to mimic the
        # pre-migration on-disk state.
        self.active = create_tenant(display_name="Active", telegram_chat_id=820001)
        self.suspended = create_tenant(display_name="Suspended", telegram_chat_id=820002)
        self.deleted = create_tenant(display_name="Deleted", telegram_chat_id=820003)

        _set_flag(self.active, False, status=Tenant.Status.ACTIVE)
        _set_flag(self.suspended, False, status=Tenant.Status.SUSPENDED)
        _set_flag(self.deleted, False, status=Tenant.Status.DELETED)

    def _run_forward(self) -> None:
        """Invoke the migration's forward function directly."""

        # Migration modules aren't importable as packages by default; pull
        # the function via importlib instead.
        import importlib

        mod = importlib.import_module("apps.tenants.migrations.0051_byo_models_enabled_default_true")
        # Pass `apps` as None — our function uses ``Tenant`` model directly
        # via ``apps.get_model`` so we provide a stub.

        class _AppsStub:
            def get_model(self, app_label, model_name):
                from apps.tenants.models import Tenant as RealTenant

                return RealTenant

        mod.enable_byo_for_fleet(_AppsStub(), schema_editor=None)

    def test_flips_active_tenant(self):
        self._run_forward()

        self.active.refresh_from_db()
        self.assertTrue(self.active.byo_models_enabled)

    def test_flips_suspended_tenant(self):
        """Suspended tenants get the flag too — their containers are
        hibernated but their config will pick up the flag if/when the
        subscription is reinstated."""
        self._run_forward()

        self.suspended.refresh_from_db()
        self.assertTrue(self.suspended.byo_models_enabled)

    def test_skips_deleted_tenants(self):
        """Deleted tenants are excluded — their containers and KV secrets
        are gone; flipping the flag is meaningless and would muddy the
        ``has_entitlement`` query surface."""
        self._run_forward()

        self.deleted.refresh_from_db()
        self.assertFalse(self.deleted.byo_models_enabled)

    def test_idempotent_when_run_twice(self):
        """Re-running the migration is a no-op for tenants already at True."""
        self._run_forward()
        # Snapshot state.
        self.active.refresh_from_db()
        self.suspended.refresh_from_db()
        first_active = self.active.byo_models_enabled
        first_suspended = self.suspended.byo_models_enabled

        # Second pass.
        self._run_forward()

        self.active.refresh_from_db()
        self.suspended.refresh_from_db()
        self.assertEqual(self.active.byo_models_enabled, first_active)
        self.assertEqual(self.suspended.byo_models_enabled, first_suspended)
        self.assertTrue(self.active.byo_models_enabled)
        self.assertTrue(self.suspended.byo_models_enabled)
        # Deleted tenants still excluded after the second pass.
        self.deleted.refresh_from_db()
        self.assertFalse(self.deleted.byo_models_enabled)


class ByoFleetMigrationDefaultTest(TestCase):
    """Schema-default coverage: ``create_tenant`` after this PR ships
    rows with ``byo_models_enabled=True`` so newly provisioned tenants
    don't need a one-shot data migration."""

    def test_new_tenant_has_byo_enabled_by_default(self):
        new_tenant = create_tenant(display_name="NewTenant", telegram_chat_id=820010)
        new_tenant.refresh_from_db()
        self.assertTrue(new_tenant.byo_models_enabled)
