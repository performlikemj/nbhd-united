"""Internal auth helper tests.

All tenant containers use a shared internal API key. Per-tenant keys were
removed 2026-02-22 (caused mass auth failures, unnecessary given network
isolation — tenant containers are internal-only in Azure).
"""
from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings

from .internal_auth import InternalAuthError, validate_internal_runtime_request


class InternalAuthHelperTest(TestCase):
    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_accepts_valid_key_and_tenant(self):
        tenant_id = validate_internal_runtime_request(
            provided_key="shared-key",
            provided_tenant_id="tenant-123",
            expected_tenant_id="tenant-123",
        )
        self.assertEqual(tenant_id, "tenant-123")

    @override_settings(NBHD_INTERNAL_API_KEY="")
    def test_rejects_when_config_missing(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="shared-key",
                provided_tenant_id="tenant-123",
            )

    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_rejects_wrong_key(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="wrong-key",
                provided_tenant_id="tenant-123",
            )

    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_rejects_mismatched_tenant(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="shared-key",
                provided_tenant_id="tenant-abc",
                expected_tenant_id="tenant-xyz",
            )

    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_rejects_missing_key(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="",
                provided_tenant_id="tenant-123",
            )

    @override_settings(NBHD_INTERNAL_API_KEY="shared-key")
    def test_rejects_missing_tenant_id(self):
        with self.assertRaises(InternalAuthError):
            validate_internal_runtime_request(
                provided_key="shared-key",
                provided_tenant_id="",
            )
