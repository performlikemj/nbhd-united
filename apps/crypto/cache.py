"""Per-process DEK cache — encryption-at-rest Phase 1 (PR3).

Maps `(tenant_id, dek_epoch) -> plaintext DEK bytes`. A DEK is immutable
once minted for a given epoch — rotation (Phase 5) mints a NEW epoch rather
than overwriting one — so a cached `(tenant, epoch)` entry never needs
invalidation; it only ever grows for the life of the process.

A cache HIT never touches Key Vault. That's deliberate: it's what lets
decrypt keep working through a Key Vault outage for any tenant whose DEK is
already warm (via pre-warm, PR4, or simply having served one request this
process lifetime already). A cache MISS does exactly one broker unwrap under
a lock, so concurrent callers racing on a cold `(tenant, epoch)` — e.g. the
poller thread and a request-handling thread waking the same tenant at once —
pay for one unwrap between them, not one each. The GIL alone doesn't
guarantee this: `unwrap_dek` is a network call and releases it.
"""

from __future__ import annotations

import threading

from apps.orchestrator import azure_client

_LOCK = threading.Lock()
_CACHE: dict[tuple[str, int], bytes] = {}


def _fetch_wrapped_dek(tenant_id: str, dek_epoch: int) -> bytes:
    """Look up the wrapped DEK bytes for `(tenant_id, dek_epoch)` in `tenant_deks`.

    Local import: `apps.tenants.models` pulls in a much heavier import graph
    (the whole `User`/`Tenant` model module) than this module otherwise
    needs, and keeping it out of the module-level imports avoids widening
    `apps.crypto.cache`'s footprint for callers that only ever hit the cache
    (never a cold path).
    """
    from apps.tenants.models import TenantDek

    row = TenantDek.objects.get(tenant_id=tenant_id, dek_epoch=dek_epoch)
    return bytes(row.wrapped_dek)


def get_dek(tenant_id: str, dek_epoch: int) -> bytes:
    """Return the plaintext DEK for `(tenant_id, dek_epoch)`.

    Cache hit -> returned with zero I/O. Cache miss -> under `_LOCK`,
    double-checked (another thread may have just populated it), then the
    wrapped DEK is fetched from `tenant_deks` and unwrapped via the decrypt
    broker (`azure_client.unwrap_dek`), stored, and returned.

    Raises whatever the lookup/unwrap raises on a miss that can't resolve —
    `TenantDek.DoesNotExist` if nothing was ever minted for this epoch, or
    the broker's own error if the KEK was purged/unreachable. A DEK that
    can't be resolved must fail closed, never silently cache nothing and
    return nothing.
    """
    tenant_id = str(tenant_id)
    dek_epoch = int(dek_epoch)
    key = (tenant_id, dek_epoch)

    dek = _CACHE.get(key)
    if dek is not None:
        return dek

    with _LOCK:
        dek = _CACHE.get(key)
        if dek is not None:
            return dek

        wrapped = _fetch_wrapped_dek(tenant_id, dek_epoch)
        dek = azure_client.unwrap_dek(tenant_id, wrapped)
        _CACHE[key] = dek
        return dek


def prime(tenant_id: str, dek_epoch: int, dek: bytes) -> None:
    """Populate the cache without a request — used by PR4's pre-warm sweep.

    Safe to call redundantly (e.g. pre-warm racing a real request that just
    cold-missed the same tenant) — a DEK is immutable per epoch, so there is
    never a "wrong" value to overwrite; this only ever needs to run once per
    `(tenant, epoch)` per process.
    """
    key = (str(tenant_id), int(dek_epoch))
    with _LOCK:
        _CACHE[key] = dek
