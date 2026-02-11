"""Internal auth helper tests."""
from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings

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

    @override_settings(NBHD_INTERNAL_API_KEY="")
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
