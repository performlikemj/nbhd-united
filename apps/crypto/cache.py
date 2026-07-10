"""Per-process DEK cache — encryption-at-rest Phase 1 (PR3).

Maps `(tenant_id, dek_epoch) -> plaintext DEK bytes`. A DEK is normally
immutable once minted for a given epoch — rotation (Phase 5) mints a NEW
epoch rather than overwriting one — so an entry is only added, never changed.
The ONE exception is a re-key: a purged tenant re-provisioned from scratch
mints a brand-new DEK at the SAME epoch 0 (`keys.mint_and_wrap_dek`'s
fresh-start path), so the process that performs the re-key must `evict` its
now-dead cached DEK. Eviction is for that deliberate re-key ONLY — a Key
Vault FAILURE must never evict, because a cached DEK stays valid across an
arbitrarily long KV outage and that immutability is what keeps decrypt
working through one.

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


def evict(tenant_id: str, dek_epoch: int) -> None:
    """Drop the cached DEK for `(tenant_id, dek_epoch)` — deliberate re-key ONLY.

    Exists solely for the fresh-start re-key path: a purged tenant that gets
    re-provisioned mints a brand-new DEK at the SAME epoch 0, so the process
    that performs the re-key must not keep serving the dead DEK it may have
    cached. This is NOT an outage/error hook — a Key Vault unwrap failure must
    NEVER evict (a cached DEK stays valid across an arbitrarily long KV outage;
    that is what keeps decrypt working through one). Evicting an entry that
    isn't cached is a no-op.

    Per-process only — this clears THIS process's cache; siblings are not
    signalled (see `keys.mint_and_wrap_dek`'s fresh-start comment).
    """
    key = (str(tenant_id), int(dek_epoch))
    with _LOCK:
        _CACHE.pop(key, None)


def evict_tenant(tenant_id: str) -> None:
    """Drop every cached epoch for `tenant_id` — deliberate re-key ONLY.

    Same contract as `evict` (never an outage hook, per-process only). The
    fresh-start re-key drops ALL of a tenant's `TenantDek` rows, so it evicts
    ALL of the tenant's cached epochs to match — Phase 1 only ever caches epoch
    0, so today this coincides with `evict(tenant_id, 0)`, but it stays correct
    once DEK rotation (Phase 5) lets multiple epochs coexist.
    """
    tid = str(tenant_id)
    with _LOCK:
        for key in [k for k in _CACHE if k[0] == tid]:
            del _CACHE[key]
