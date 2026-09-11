"""Canary-gated, process-local storage account keys and bounded auth refresh."""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

from azure.core.exceptions import ClientAuthenticationError
from azure.storage.fileshare import StorageErrorCode
from django.conf import settings

logger = logging.getLogger(__name__)
_PROCESS_START = int(time.time())
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


@dataclass(frozen=True)
class KeyLease:
    """A fetched key; cached means eligible and stored, including a cold fill."""

    key: str = field(repr=False)
    generation: int
    cached: bool
    _account: tuple | None = field(default=None, repr=False)


@dataclass
class _AccountState:
    lock: object = field(default_factory=threading.Lock)
    entry: tuple[KeyLease, float] | None = None
    generation: int = 0
    auth_refresh: bool = False


_accounts = {}
_accounts_lock = threading.Lock()
_summary_lock = threading.Lock()
_hits = 0
_bypasses = 0
_last_summary = time.monotonic()


def storage_key_cache_enabled(tenant_id) -> bool:
    raw = str(getattr(settings, "AZURE_STORAGE_KEY_CACHE_TENANT_IDS", "") or "")
    allowed = {part.strip().lower() for part in raw.split(",")}
    tenant = str(tenant_id).lower()
    return "*" in allowed or bool(_UUID.fullmatch(tenant) and tenant in allowed)


def key_cache_ttl_seconds(value) -> int:
    """Parse the env setting, also accepting runtime integer overrides."""
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return 300
    return value if type(value) is int and value > 0 else 300


def _count(*, hit=False, bypass=False):
    global _hits, _bypasses, _last_summary
    summary = None
    with _summary_lock:
        _hits += int(hit)
        _bypasses += int(bypass)
        now = time.monotonic()
        if now - _last_summary >= 600:
            summary = (_hits, _bypasses)
            _hits = 0
            _bypasses = 0
            _last_summary = now
    if summary is not None:
        logger.info(
            "storage_key cache summary hits=%d bypasses=%d replica=%s proc=%d-%d",
            *summary,
            os.environ.get("CONTAINER_APP_REPLICA_NAME") or "-",
            os.getpid(),
            _PROCESS_START,
        )


def _fetch(account_name):
    # Keep the local import: callers/tests patch azure_client at call time.
    from apps.orchestrator.azure_client import get_storage_client

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(settings.AZURE_RESOURCE_GROUP, account_name)
    return keys.keys[0].value


def acquire_account_key(tenant_id) -> KeyLease:
    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not storage_key_cache_enabled(tenant_id):
        _count(bypass=True)
        return KeyLease(_fetch(account_name), 0, False)

    account = (settings.AZURE_SUBSCRIPTION_ID, settings.AZURE_RESOURCE_GROUP, account_name)
    with _accounts_lock:
        state = _accounts.get(account)
        if state is None:
            state = _AccountState()
            _accounts[account] = state
    entry = state.entry
    if entry is not None and time.monotonic() < entry[1]:
        _count(hit=True)
        return entry[0]

    fetch_info = None
    try:
        with state.lock:
            entry = state.entry
            if entry is None or time.monotonic() >= entry[1]:
                source = "auth_refresh" if state.auth_refresh else "expired" if entry else "cold"
                started = time.monotonic()
                try:
                    key = _fetch(account_name)
                finally:
                    fetch_info = (source, (time.monotonic() - started) * 1000)
                state.generation += 1
                lease = KeyLease(key, state.generation, bool(key), account)
                if key:
                    expires_at = time.monotonic() + key_cache_ttl_seconds(
                        getattr(settings, "AZURE_STORAGE_KEY_CACHE_TTL_SECONDS", 300)
                    )
                    state.entry = (lease, expires_at)
                    state.auth_refresh = False
                else:
                    state.entry = None
            else:
                lease = entry[0]
    finally:
        # This includes failed fetches, without exposing their exception bodies.
        if fetch_info is not None:
            source, duration = fetch_info
            logger.info(
                "storage_key fetch source=%s tenant=%s replica=%s proc=%d-%d ms=%.3f",
                source,
                tenant_id,
                os.environ.get("CONTAINER_APP_REPLICA_NAME") or "-",
                os.getpid(),
                _PROCESS_START,
                duration,
            )
    _count(hit=fetch_info is None)
    return lease


def invalidate(lease: KeyLease) -> None:
    if not lease.cached:
        return
    with _accounts_lock:
        state = _accounts.get(lease._account)
    if state is not None:
        with state.lock:
            if state.entry is not None and state.entry[0].generation == lease.generation:
                state.entry = None
                state.auth_refresh = True


def run_with_lease(tenant_id, lease, op):
    """Return (result, lease), retaining a refreshed lease between upload stages."""
    try:
        return op(lease.key), lease
    except ClientAuthenticationError as exc:
        if not lease.cached or getattr(exc, "error_code", None) != StorageErrorCode.AUTHENTICATION_FAILED:
            raise
    invalidate(lease)
    lease = acquire_account_key(tenant_id)
    return op(lease.key), lease


def run_with_key(tenant_id, op):
    result, _ = run_with_lease(tenant_id, acquire_account_key(tenant_id), op)
    return result
