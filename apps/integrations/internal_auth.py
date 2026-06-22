"""Internal runtime-to-control-plane auth helpers.

Architecture (2026-05-12 Phase 1 → 2026-06-22 Phase 1d):
    Tenant OpenClaw containers authenticate to Django internal endpoints
    via `X-NBHD-Internal-Key` + `X-NBHD-Tenant-Id` headers. The validator
    here is the single chokepoint for every internal route (journal/fuel/
    finance/integrations/cron/router/platform_logs/tenants runtime views).

    Per-tenant validation (only mode):
        The provided key MUST match the tenant's `Tenant.internal_api_key`
        (constant-time compare). Per-tenant scope — a key leaked from
        container A cannot authenticate as tenant B. Each container sources
        its key from its own per-tenant Key Vault secret.

    History — the legacy global fallback:
        During the fleet migration (Phase 1a–1c) this validator ALSO
        accepted the shared `settings.NBHD_INTERNAL_API_KEY` as a fallback,
        so pre-migration containers kept working between the DB key update
        and the container revision rollout. That fallback was REMOVED in
        Phase 1d (2026-06-22) after a 7-day audit showed zero
        `key_provenance=legacy_global` hits across the active fleet. The
        deliberately gradual approach (vs the 2026-02-22 flag-day removal)
        avoided the "mass auth failures during provisioning" outage.

    Note: `settings.NBHD_INTERNAL_API_KEY` is still defined and used
    elsewhere (the Django→container gateway-token fallback in
    `apps/cron/gateway_client.py` + `apps/router/poller.py`, and as the
    container's own `NBHD_INTERNAL_API_KEY` env var name). Phase 1d removed
    only its acceptance as an inbound runtime-request credential here.
"""

from __future__ import annotations

import logging
import secrets

logger = logging.getLogger("nbhd.internal_auth")


# Key-provenance enum values used in audit log structured fields.
# PROVENANCE_LEGACY_GLOBAL ("legacy_global") was retired in Phase 1d
# (2026-06-22) when the global-key fallback was removed.
PROVENANCE_PER_TENANT = "per_tenant"
PROVENANCE_NONE = "none"

# Outcome enum values.
OUTCOME_SUCCESS = "success"
OUTCOME_BAD_KEY = "bad_key"
OUTCOME_TENANT_MISMATCH = "tenant_mismatch"
OUTCOME_MISSING_KEY = "missing_key"
OUTCOME_MISSING_TENANT = "missing_tenant"
OUTCOME_NO_KEY_CONFIGURED = "no_key_configured"


class InternalAuthError(PermissionError):
    """Raised when internal runtime auth validation fails."""


def _emit_audit(
    *,
    provided_tenant_id: str,
    expected_tenant_id: str,
    key_provenance: str,
    outcome: str,
) -> None:
    """Emit a structured audit event for an internal-auth attempt.

    Logged at INFO so Azure Log Analytics ingests every event. The level
    is intentionally low — auth failures are not exceptional here, they're
    operational signal. A separate alert can be wired on
    `outcome != "success"` patterns when the fleet is fully migrated.

    The provenance/outcome are folded into the message STRING, not just the
    `extra` dict: the production log formatter is plain-text
    ("{levelname} {asctime} {name} {message}") and does not render `extra`
    fields, so a fields-only event reaches Log Analytics as a bare
    "internal_auth_event" with no provenance. Without this, the Phase 1d
    gate ("zero key_provenance=legacy_global hits for 48h") is unobservable.
    The `extra` dict is retained for any future JSON/structured handler.
    """
    logger.info(
        "internal_auth_event key_provenance=%s outcome=%s provided_tenant_id=%s expected_tenant_id=%s",
        key_provenance,
        outcome,
        provided_tenant_id or "-",
        expected_tenant_id or "-",
        extra={
            "event": "internal_auth_event",
            "provided_tenant_id": provided_tenant_id,
            "expected_tenant_id": expected_tenant_id,
            "key_provenance": key_provenance,
            "outcome": outcome,
        },
    )


def _lookup_tenant_key(tenant_id: str) -> str | None:
    """Return the per-tenant internal_api_key for `tenant_id`, or None.

    Returns None for non-UUID tenant_ids (test fixtures use strings like
    "tenant-123" which can never resolve to a real Tenant) and for tenants
    that haven't been migrated yet (internal_api_key still empty).
    """
    # Import here to avoid an import cycle at module load — internal_auth
    # is imported by many runtime views during Django app initialization.
    from django.core.exceptions import ValidationError

    from apps.tenants.models import Tenant

    try:
        tenant = Tenant.objects.only("internal_api_key").get(id=tenant_id)
    except (Tenant.DoesNotExist, ValueError, ValidationError):
        # ValidationError fires from the UUIDField when tenant_id isn't a
        # valid UUID (raised during query-prep before the DB hit).
        # ValueError is a defensive belt — older Django versions used it
        # for the same path.
        return None

    key = (tenant.internal_api_key or "").strip()
    return key or None


def validate_internal_runtime_request(
    provided_key: str,
    provided_tenant_id: str,
    expected_tenant_id: str | None = None,
) -> str:
    """Validate that a runtime request carries the correct internal key.

    Returns the tenant id from headers when valid.
    Raises InternalAuthError on any failure. Every attempt is audit-logged
    (success and failure) with key_provenance + outcome.
    """
    tenant_id = (provided_tenant_id or "").strip()
    expected_id = (expected_tenant_id or "").strip()

    if not provided_key:
        _emit_audit(
            provided_tenant_id=tenant_id,
            expected_tenant_id=expected_id,
            key_provenance=PROVENANCE_NONE,
            outcome=OUTCOME_MISSING_KEY,
        )
        raise InternalAuthError("Missing internal auth key")

    if not tenant_id:
        _emit_audit(
            provided_tenant_id="",
            expected_tenant_id=expected_id,
            key_provenance=PROVENANCE_NONE,
            outcome=OUTCOME_MISSING_TENANT,
        )
        raise InternalAuthError("Missing tenant id header")

    if expected_tenant_id is not None and tenant_id != expected_tenant_id:
        _emit_audit(
            provided_tenant_id=tenant_id,
            expected_tenant_id=expected_id,
            key_provenance=PROVENANCE_NONE,
            outcome=OUTCOME_TENANT_MISMATCH,
        )
        raise InternalAuthError("Tenant scope mismatch")

    # Phase 1d (2026-06-22): per-tenant key is the ONLY accepted credential.
    # The legacy global fallback was removed after a 7-day audit showed zero
    # `key_provenance=legacy_global` hits across the active fleet. A tenant
    # with no per-tenant key (or a tenant_id that resolves to no Tenant row)
    # is rejected outright — there is no shared key to fall back to.
    per_tenant_key = _lookup_tenant_key(tenant_id)
    if per_tenant_key is None:
        _emit_audit(
            provided_tenant_id=tenant_id,
            expected_tenant_id=expected_id,
            key_provenance=PROVENANCE_NONE,
            outcome=OUTCOME_NO_KEY_CONFIGURED,
        )
        raise InternalAuthError("No per-tenant internal key configured for tenant")

    if secrets.compare_digest(provided_key, per_tenant_key):
        _emit_audit(
            provided_tenant_id=tenant_id,
            expected_tenant_id=expected_id,
            key_provenance=PROVENANCE_PER_TENANT,
            outcome=OUTCOME_SUCCESS,
        )
        return tenant_id

    _emit_audit(
        provided_tenant_id=tenant_id,
        expected_tenant_id=expected_id,
        key_provenance=PROVENANCE_NONE,
        outcome=OUTCOME_BAD_KEY,
    )
    raise InternalAuthError("Invalid internal auth key")
