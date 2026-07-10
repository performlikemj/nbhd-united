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

import logging
import os

from apps.orchestrator import azure_client
from apps.tenants.models import Tenant, TenantDek

logger = logging.getLogger(__name__)


def mint_and_wrap_dek(tenant: Tenant) -> TenantDek:
    """Ensure a tenant has a usable epoch-0 DEK, minting/recovering as needed.

    Provisioning is re-entrant: a cancelled subscriber's Tenant row is REUSED
    when they resubscribe, and deprovision soft-deletes (then, after the grace
    window, purges) the tenant's KEK while KEEPING its ``TenantDek`` row. So the
    old "a row already exists -> no-op reuse" rule pointed a re-provisioned
    tenant at a DEAD KEK. Instead, branch on the KEK's actual liveness:

      - live        -> reuse the existing row unchanged (the steady-state path,
                       and the safe outcome of a QStash provision retry).
      - recoverable -> the KEK is soft-deleted but still inside its recovery
                       window; recover it and reuse the row, so an accidental or
                       undone deprovision comes back WITH its data intact.
      - absent      -> the KEK was purged: every ciphertext wrapped under it is
                       already cryptographically shredded by design. Drop the
                       stale row(s) and mint a FRESH epoch-0 DEK + KEK.

    Fresh material MUST land at epoch 0 (never an incremented epoch): Phase 1
    ``box.encrypt`` always targets epoch 0, so a purged tenant's stale rows are
    DELETED rather than epoch-bumped — otherwise new writes would encrypt at
    epoch 0 against a key stored at some other epoch and never decrypt.
    """
    existing = TenantDek.objects.filter(tenant=tenant, dek_epoch=0).first()
    if existing is not None:
        state = azure_client.kek_liveness(tenant.id)
        if state == "live":
            logger.info("crypto: DEK reuse for tenant %s (KEK live)", tenant.id)
            return existing
        if state == "recoverable":
            azure_client.recover_kek(tenant.id)
            logger.info("crypto: DEK recovered for tenant %s (KEK restored from grace window)", tenant.id)
            return existing
        # "absent": KEK purged/gone -> prior ciphertext is unrecoverable by
        # design. Re-key from scratch at epoch 0.
        TenantDek.objects.filter(tenant=tenant).delete()
        logger.info("crypto: DEK fresh-start for tenant %s (prior KEK purged; stale rows dropped)", tenant.id)

    return _mint_fresh_dek(tenant)


def _mint_fresh_dek(tenant: Tenant) -> TenantDek:
    """Mint a fresh KEK + wrapped epoch-0 DEK and persist the ``TenantDek`` row.

    Assumes no live epoch-0 row exists for the tenant — the caller has already
    reused/recovered a live KEK, or dropped the stale rows left by a purged one.
    """
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
