"""DEK (Data Encryption Key) key service — encryption-at-rest Phase 1.

Mints, wraps, and unwraps each tenant's Data Encryption Key. Callers should
go through this module rather than touching ``apps.tenants.models.TenantDek``
or ``apps.orchestrator.azure_client`` directly — this is the seam.

Phase 1 has no per-process cache yet (that lands in Phase 1 PR3 as
``apps.crypto.cache``); ``unwrap_dek_for`` calls the decrypt broker directly
on every invocation so the service is usable/testable now. PR3 wires a cache
in front of it so repeated unwraps of the same (tenant, epoch) become free
after the first cold miss.
"""

from __future__ import annotations

import os

from apps.orchestrator import azure_client
from apps.tenants.models import Tenant, TenantDek


def mint_and_wrap_dek(tenant: Tenant) -> TenantDek:
    """Idempotently mint a tenant's epoch-0 DEK, wrapped under a fresh KEK.

    Returns the existing epoch-0 row if one already exists — NEVER re-mints.
    Phase 1 has no rotation/re-encryption logic, so minting a second DEK for
    an existing epoch would silently orphan any ciphertext already encrypted
    under the first one.
    """
    existing = TenantDek.objects.filter(tenant=tenant, dek_epoch=0).first()
    if existing is not None:
        return existing

    azure_client.create_tenant_kek(tenant.id)
    dek = os.urandom(32)
    wrapped, kek_version = azure_client.wrap_dek(tenant.id, dek)
    return TenantDek.objects.create(
        tenant=tenant,
        dek_epoch=0,
        wrapped_dek=wrapped,
        kek_version=kek_version,
    )


def get_wrapped_dek(tenant: Tenant, epoch: int = 0) -> tuple[bytes, str]:
    """Return ``(wrapped_dek, kek_version)`` for a tenant's DEK at ``epoch``.

    Raises ``TenantDek.DoesNotExist`` if nothing has been minted at that
    epoch yet.
    """
    row = TenantDek.objects.get(tenant=tenant, dek_epoch=epoch)
    return bytes(row.wrapped_dek), row.kek_version


def unwrap_dek_for(tenant: Tenant, epoch: int = 0) -> bytes:
    """Return the plaintext DEK for a tenant at ``epoch``.

    Phase 1: calls the decrypt broker directly, no cache in front yet.
    Fail-closed — raises on a missing row, a purged KEK, or a broker error;
    never returns garbage.
    """
    wrapped, _kek_version = get_wrapped_dek(tenant, epoch)
    return azure_client.unwrap_dek(tenant.id, wrapped)
