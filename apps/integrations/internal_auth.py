"""Internal runtime-to-control-plane auth helpers."""
from __future__ import annotations

import secrets

from django.conf import settings


class InternalAuthError(PermissionError):
    """Raised when internal runtime auth validation fails."""


def validate_internal_runtime_request(
    provided_key: str,
    provided_tenant_id: str,
    expected_tenant_id: str | None = None,
) -> str:
    """Validate shared-key internal auth and tenant scope binding.

    Returns the tenant id from headers when valid.
    """
    configured_key = getattr(settings, "NBHD_INTERNAL_API_KEY", "")
    if not configured_key:
        raise InternalAuthError("NBHD_INTERNAL_API_KEY is not configured")

    if not provided_key:
        raise InternalAuthError("Missing internal auth key")

    if not secrets.compare_digest(provided_key, configured_key):
        raise InternalAuthError("Invalid internal auth key")

    tenant_id = (provided_tenant_id or "").strip()
    if not tenant_id:
        raise InternalAuthError("Missing tenant id header")

    if expected_tenant_id is not None and tenant_id != expected_tenant_id:
        raise InternalAuthError("Tenant scope mismatch")

    return tenant_id
