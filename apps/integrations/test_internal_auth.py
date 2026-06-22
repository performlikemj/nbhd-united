"""Internal auth helper tests.

Phase 1 (2026-05-12): dual-validation against per-tenant `Tenant.internal_api_key`
with legacy `settings.NBHD_INTERNAL_API_KEY` as fallback.

Phase 1d (2026-06-22): the legacy global fallback was REMOVED. The per-tenant
key is now the sole accepted credential; a tenant with no per-tenant key (or a
tenant_id resolving to no Tenant row) is rejected outright. These tests assert
the fallback is gone and that the global key is no longer honored. Every
validation attempt still emits a structured audit event via the
`nbhd.internal_auth` logger.
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
    PROVENANCE_NONE,
    PROVENANCE_PER_TENANT,
    InternalAuthError,
    validate_internal_runtime_request,
)

# ── No global fallback (Phase 1d) ──────────────────────────────────────────
# A global NBHD_INTERNAL_API_KEY is set, but it must NOT be accepted as an
# inbound runtime credential. Tenant ids that don't resolve to a Tenant row
# (the test fixtures use non-UUID strings) have no per-tenant key, so every
# call here is rejected — there is no shared key to fall back to.


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class NoGlobalFallbackTest(SimpleTestCase):
    """Phase 1d: the legacy global key is no longer a valid credential."""

    def test_global_key_is_rejected(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="shared-key",
                provided_tenant_id="tenant-123",
                expected_tenant_id="tenant-123",
            )

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
    def test_rejects_when_no_per_tenant_key(self):
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
    def test_per_tenant_set_but_global_value_is_rejected(self):
        """Phase 1d: the global key is NO LONGER a fallback. A tenant with a
        per-tenant key set rejects the global key rather than accepting it."""
        tenant = _make_tenant(internal_api_key="per-tenant-secret")
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="global-key",
                provided_tenant_id=str(tenant.id),
                expected_tenant_id=str(tenant.id),
            )

    @override_settings(NBHD_INTERNAL_API_KEY="global-key")
    def test_rejects_when_per_tenant_key_does_not_match(self):
        tenant = _make_tenant(internal_api_key="per-tenant-secret")
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="totally-wrong",
                provided_tenant_id=str(tenant.id),
                expected_tenant_id=str(tenant.id),
            )

    @override_settings(NBHD_INTERNAL_API_KEY="global-key")
    def test_tenant_without_per_tenant_key_is_rejected(self):
        """A real Tenant row with an empty internal_api_key has no credential
        to validate against — rejected (previously accepted via global key)."""
        tenant = _make_tenant(internal_api_key="")
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="global-key",
                provided_tenant_id=str(tenant.id),
                expected_tenant_id=str(tenant.id),
            )


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

    def test_global_key_no_longer_accepted_logged(self):
        """Phase 1d regression guard: a container presenting the old global
        key against a tenant that has a per-tenant key is rejected as bad_key
        — never `legacy_global` success (that provenance no longer exists)."""
        tenant = _make_tenant(internal_api_key="per-tenant-secret")
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="global-key",
                provided_tenant_id=str(tenant.id),
                expected_tenant_id=str(tenant.id),
            )
        )
        self.assertEqual(rec.key_provenance, PROVENANCE_NONE)
        self.assertEqual(rec.outcome, OUTCOME_BAD_KEY)
        self.assertNotIn("legacy_global", rec.getMessage())

    def test_bad_key_logged(self):
        tenant = _make_tenant(internal_api_key="per-tenant-secret")
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

    def test_no_per_tenant_key_logged(self):
        """A tenant_id with no resolvable per-tenant key emits
        no_key_configured (Phase 1d: there is no global fallback to try)."""
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="anything",
                provided_tenant_id="tenant-123",
            )
        )
        self.assertEqual(rec.outcome, OUTCOME_NO_KEY_CONFIGURED)

    def test_rendered_message_carries_provenance_for_log_analytics(self):
        """The provenance/outcome must appear in the RENDERED message, not
        just `extra`. Production's formatter is plain-text and drops `extra`,
        and the internal-auth audit greps the message string. This guards
        against regressing to a fields-only emit that makes the audit
        silently unobservable.
        """
        tenant = _make_tenant(internal_api_key="per-tenant-secret")
        rec = self._capture(
            lambda: validate_internal_runtime_request(
                provided_key="per-tenant-secret",
                provided_tenant_id=str(tenant.id),
                expected_tenant_id=str(tenant.id),
            )
        )
        message = rec.getMessage()
        self.assertIn(f"key_provenance={PROVENANCE_PER_TENANT}", message)
        self.assertIn(f"outcome={OUTCOME_SUCCESS}", message)
        self.assertIn(str(tenant.id), message)
