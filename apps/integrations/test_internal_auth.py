"""Internal auth helper tests."""
from __future__ import annotations

import hashlib

from django.test import TestCase
from django.test.utils import override_settings

from apps.tenants.services import create_tenant

from .internal_auth import InternalAuthError, validate_internal_runtime_request


class InternalAuthHelperTest(TestCase):
    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_validate_internal_runtime_request_accepts_valid_key_and_tenant(self):
        tenant_id = validate_internal_runtime_request(
            provided_key="shared-key",
            provided_tenant_id="tenant-123",
            expected_tenant_id="tenant-123",
        )
        self.assertEqual(tenant_id, "tenant-123")

    @override_settings(
        NBHD_INTERNAL_API_KEY="",
        NBHD_INTERNAL_API_KEY_FALLBACK_ENABLED=True,
    )
    def test_validate_internal_runtime_request_rejects_when_config_missing(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="shared-key",
                provided_tenant_id="tenant-123",
            )

    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_validate_internal_runtime_request_rejects_wrong_key(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="wrong-key",
                provided_tenant_id="tenant-123",
            )

    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_validate_internal_runtime_request_rejects_mismatched_tenant(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="shared-key",
                provided_tenant_id="tenant-abc",
                expected_tenant_id="tenant-xyz",
            )


class PerTenantInternalAuthTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Auth", telegram_chat_id=999111)
        self.plaintext_key = "per-tenant-secret-key-abc123"
        self.tenant.internal_api_key_hash = hashlib.sha256(
            self.plaintext_key.encode("utf-8")
        ).hexdigest()
        self.tenant.save(update_fields=["internal_api_key_hash"])

    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_per_tenant_key_accepted(self):
        tid = validate_internal_runtime_request(
            provided_key=self.plaintext_key,
            provided_tenant_id=str(self.tenant.id),
            expected_tenant_id=str(self.tenant.id),
        )
        self.assertEqual(tid, str(self.tenant.id))

    @override_settings(
        NBHD_INTERNAL_API_KEY="shared-key",
        NBHD_INTERNAL_API_KEY_FALLBACK_ENABLED=False,
    )
    def test_wrong_key_rejected_with_fallback_disabled(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="wrong-key",
                provided_tenant_id=str(self.tenant.id),
                expected_tenant_id=str(self.tenant.id),
            )

    @override_settings(
        NBHD_INTERNAL_API_KEY="shared-key",
        NBHD_INTERNAL_API_KEY_FALLBACK_ENABLED=True,
    )
    def test_shared_key_fallback_works_when_enabled(self):
        tid = validate_internal_runtime_request(
            provided_key="shared-key",
            provided_tenant_id=str(self.tenant.id),
            expected_tenant_id=str(self.tenant.id),
        )
        self.assertEqual(tid, str(self.tenant.id))

    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_tenant_without_hash_uses_shared_key(self):
        tenant2 = create_tenant(display_name="Legacy", telegram_chat_id=999222)
        tid = validate_internal_runtime_request(
            provided_key="shared-key",
            provided_tenant_id=str(tenant2.id),
            expected_tenant_id=str(tenant2.id),
        )
        self.assertEqual(tid, str(tenant2.id))
