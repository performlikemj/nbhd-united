"""Internal auth helper tests.

Phase 1 (2026-05-12): dual-validation against per-tenant `Tenant.internal_api_key`
with legacy `settings.NBHD_INTERNAL_API_KEY` as fallback. Every validation
attempt emits a structured audit event via `nbhd.internal_auth` logger.
"""

from __future__ import annotations

import logging
import uuid

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.test.utils import override_settings

from apps.tenants.models import Tenant

from .internal_auth import (
    OUTCOME_BAD_KEY,
    OUTCOME_MISSING_KEY,
    OUTCOME_MISSING_TENANT,
    OUTCOME_NO_KEY_CONFIGURED,
    OUTCOME_SUCCESS,
    OUTCOME_TENANT_MISMATCH,
    PROVENANCE_LEGACY_GLOBAL,
    PROVENANCE_NONE,
    PROVENANCE_PER_TENANT,
    InternalAuthError,
    validate_internal_runtime_request,
)

# ── Legacy path (no per-tenant key set, tenant_id is a non-UUID string) ────
# These mirror the pre-Phase-1 tests; they exercise the global-key fallback.


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class LegacyGlobalKeyPathTest(SimpleTestCase):
    """When no Tenant has internal_api_key set, the global key path is taken."""

    def test_accepts_valid_global_key(self):
        tenant_id = validate_internal_runtime_request(
            provided_key="shared-key",
            provided_tenant_id="tenant-123",
            expected_tenant_id="tenant-123",
        )
        self.assertEqual(tenant_id, "tenant-123")

    def test_rejects_wrong_key(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="wrong-key",
                provided_tenant_id="tenant-123",
            )

    def test_rejects_mismatched_tenant(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="shared-key",
                provided_tenant_id="tenant-abc",
                expected_tenant_id="tenant-xyz",
            )

    def test_rejects_missing_key(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="",
                provided_tenant_id="tenant-123",
            )

    def test_rejects_missing_tenant_id(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="shared-key",
                provided_tenant_id="",
            )


@override_settings(NBHD_INTERNAL_API_KEY="")
class MissingConfigTest(SimpleTestCase):
    def test_rejects_when_no_key_configured_and_no_per_tenant(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="some-key",
                provided_tenant_id="tenant-123",
            )


# ── Per-tenant path (Tenant.internal_api_key set, real UUIDs) ──────────────


def _make_tenant(internal_api_key: str = "") -> Tenant:
    User = get_user_model()
    user = User.objects.create_user(
        username=f"u-{uuid.uuid4()}",
        email=f"u-{uuid.uuid4()}@example.com",
    )
    return Tenant.objects.create(
        id=uuid.uuid4(),
        user=user,
        internal_api_key=internal_api_key,
    )


class PerTenantKeyPathTest(TestCase):
    @override_settings(NBHD_INTERNAL_API_KEY="global-key")
    def test_accepts_per_tenant_key_when_set(self):
        tenant = _make_tenant(internal_api_key="per-tenant-secret")
        result = validate_internal_runtime_request(
            provided_key="per-tenant-secret",
            provided_tenant_id=str(tenant.id),
            expected_tenant_id=str(tenant.id),
        )
        self.assertEqual(result, str(tenant.id))

    @override_settings(NBHD_INTERNAL_API_KEY="global-key")
    def test_per_tenant_set_but_wrong_value_still_falls_back_to_global(self):
        """During migration the global key remains a valid fallback even
        for tenants who already have a per-tenant key set. This is what
        keeps containers alive between the DB update and the revision
        rollout pushing the new env value. Phase 1d removes the fallback."""
        tenant = _make_tenant(internal_api_key="per-tenant-secret")
        result = validate_internal_runtime_request(
            provided_key="global-key",
            provided_tenant_id=str(tenant.id),
            expected_tenant_id=str(tenant.id),
        )
        self.assertEqual(result, str(tenant.id))

    @override_settings(NBHD_INTERNAL_API_KEY="global-key")
    def test_rejects_when_neither_per_tenant_nor_global_matches(self):
        tenant = _make_tenant(internal_api_key="per-tenant-secret")
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="totally-wrong",
                provided_tenant_id=str(tenant.id),
                expected_tenant_id=str(tenant.id),
            )

    @override_settings(NBHD_INTERNAL_API_KEY="global-key")
    def test_tenant_without_per_tenant_key_uses_global(self):
        tenant = _make_tenant(internal_api_key="")
        result = validate_internal_runtime_request(
            provided_key="global-key",
            provided_tenant_id=str(tenant.id),
            expected_tenant_id=str(tenant.id),
        )
        self.assertEqual(result, str(tenant.id))


# ── Audit logging ───────────────────────────────────────────────────────────


@override_settings(NBHD_INTERNAL_API_KEY="global-key")
class AuditLogTest(TestCase):
    """Every validation attempt emits a structured event on the
    `nbhd.internal_auth` logger with key_provenance + outcome fields.
    """

    def _capture(self, callable_):
        with self.assertLogs("nbhd.internal_auth", level=logging.INFO) as cm:
            try:
                callable_()
            except InternalAuthError:
                pass
        # Pull the structured `extra` dict off the captured LogRecord
        records = [r for r in cm.records if getattr(r, "event", "") == "internal_auth_event"]
        self.assertEqual(len(records), 1, "expected exactly one audit event")
        return records[0]

    def test_per_tenant_success_logged(self):
        tenant = _make_tenant(internal_api_key="per-tenant-secret")
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="per-tenant-secret",
                provided_tenant_id=str(tenant.id),
                expected_tenant_id=str(tenant.id),
            )
        )
        self.assertEqual(rec.key_provenance, PROVENANCE_PER_TENANT)
        self.assertEqual(rec.outcome, OUTCOME_SUCCESS)
        self.assertEqual(rec.provided_tenant_id, str(tenant.id))

    def test_legacy_global_success_logged(self):
        tenant = _make_tenant(internal_api_key="")
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="global-key",
                provided_tenant_id=str(tenant.id),
                expected_tenant_id=str(tenant.id),
            )
        )
        self.assertEqual(rec.key_provenance, PROVENANCE_LEGACY_GLOBAL)
        self.assertEqual(rec.outcome, OUTCOME_SUCCESS)

    def test_bad_key_logged(self):
        tenant = _make_tenant(internal_api_key="")
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="wrong",
                provided_tenant_id=str(tenant.id),
                expected_tenant_id=str(tenant.id),
            )
        )
        self.assertEqual(rec.key_provenance, PROVENANCE_NONE)
        self.assertEqual(rec.outcome, OUTCOME_BAD_KEY)

    def test_missing_key_logged(self):
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="",
                provided_tenant_id="tenant-123",
            )
        )
        self.assertEqual(rec.outcome, OUTCOME_MISSING_KEY)

    def test_missing_tenant_logged(self):
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="global-key",
                provided_tenant_id="",
            )
        )
        self.assertEqual(rec.outcome, OUTCOME_MISSING_TENANT)

    def test_tenant_mismatch_logged(self):
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="global-key",
                provided_tenant_id="tenant-a",
                expected_tenant_id="tenant-b",
            )
        )
        self.assertEqual(rec.outcome, OUTCOME_TENANT_MISMATCH)

    @override_settings(NBHD_INTERNAL_API_KEY="")
    def test_no_key_configured_logged(self):
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="anything",
                provided_tenant_id="tenant-123",
            )
        )
        self.assertEqual(rec.outcome, OUTCOME_NO_KEY_CONFIGURED)
