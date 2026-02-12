"""Internal runtime-to-control-plane auth helpers."""
from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError


class InternalAuthError(PermissionError):
    """Raised when internal runtime auth validation fails."""


def validate_internal_runtime_request(
    provided_key: str,
    provided_tenant_id: str,
    expected_tenant_id: str | None = None,
) -> str:
    """Validate internal auth (per-tenant key preferred, shared key fallback).

    Returns the tenant id from headers when valid.
    """
    if not provided_key:
        raise InternalAuthError("Missing internal auth key")

    tenant_id = (provided_tenant_id or "").strip()
    if not tenant_id:
        raise InternalAuthError("Missing tenant id header")

    if expected_tenant_id is not None and tenant_id != expected_tenant_id:
        raise InternalAuthError("Tenant scope mismatch")

    # --- Per-tenant key check ---
    from apps.tenants.models import Tenant

    try:
        stored_hash = (
            Tenant.objects.filter(id=tenant_id)
            .values_list("internal_api_key_hash", flat=True)
            .first()
        )
    except (ValueError, ValidationError):
        stored_hash = None

    if stored_hash:
        provided_hash = hashlib.sha256(provided_key.encode("utf-8")).hexdigest()
        if secrets.compare_digest(provided_hash, stored_hash):
            return tenant_id

    # --- Shared key fallback ---
    if not getattr(settings, "NBHD_INTERNAL_API_KEY_FALLBACK_ENABLED", True):
        raise InternalAuthError("Invalid internal auth key")

    configured_key = getattr(settings, "NBHD_INTERNAL_API_KEY", "")
    if not configured_key:
        raise InternalAuthError("NBHD_INTERNAL_API_KEY is not configured")

    if not secrets.compare_digest(provided_key, configured_key):
        raise InternalAuthError("Invalid internal auth key")

    return tenant_id
