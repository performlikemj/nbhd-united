"""Internal runtime-to-control-plane auth helpers.

Architecture (2026-02-22):
    All tenant OpenClaw containers share a single internal API key stored in
    Azure Key Vault (`nbhd-internal-api-key`). This is safe because tenant
    containers are deployed with `external: False` — they are only reachable
    from within the Azure Container Apps environment, not from the public
    internet. The only path to runtime endpoints is:

        Public internet → Django (external) → tenant container (internal)

    A previous per-tenant key scheme was removed because it caused mass auth
    failures during provisioning and added complexity without meaningful
    security benefit given the network isolation.
"""
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
    """Validate that a runtime request carries the correct shared internal key.

    Returns the tenant id from headers when valid.
    Raises InternalAuthError on any failure.
    """
    if not provided_key:
        raise InternalAuthError("Missing internal auth key")

    tenant_id = (provided_tenant_id or "").strip()
    if not tenant_id:
        raise InternalAuthError("Missing tenant id header")

    if expected_tenant_id is not None and tenant_id != expected_tenant_id:
        raise InternalAuthError("Tenant scope mismatch")

    # Validate against the shared internal API key
    configured_key = getattr(settings, "NBHD_INTERNAL_API_KEY", "")
    if not configured_key:
        raise InternalAuthError("NBHD_INTERNAL_API_KEY is not configured")

    if not secrets.compare_digest(provided_key, configured_key):
        raise InternalAuthError("Invalid internal auth key")

    return tenant_id
