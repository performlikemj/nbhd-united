"""Per-tenant recall blind-index search-key lifecycle.

The key is a standalone 32-byte secret in the platform Key Vault. It is
cached only in this process, and the mock is deliberately stateful so tests
can exercise soft-delete and recovery instead of re-deriving deleted keys.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
from typing import TYPE_CHECKING

from django.conf import settings

from .azure_client import _get_provisioner_credential, _is_mock

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

_KEY_BYTES = 32
_SECRET_SUFFIX = "recall-search-key"
_LOCK = threading.Lock()
_CACHE: dict[str, bytes] = {}

# Same stateful-mock principle as azure_client._MOCK_KEK_REGISTRY. Deleted
# entries retain their random key during the recovery window; recovery restores
# that exact key, while a future purge hook can remove the entry permanently.
_MOCK_RECALL_SEARCH_KEY_REGISTRY: dict[str, dict[str, object]] = {}


def _secret_name(tenant: Tenant) -> str:
    prefix = (getattr(tenant, "key_vault_prefix", "") or "").strip()
    if not prefix:
        raise ValueError(
            f"Tenant {getattr(tenant, 'id', '?')} has no key_vault_prefix — cannot build recall search-key secret name"
        )
    return f"{prefix}-{_SECRET_SUFFIX}"


def _secret_client():
    from azure.keyvault.secrets import SecretClient

    vault_name = str(getattr(settings, "AZURE_KEY_VAULT_NAME", "") or "").strip()
    if not vault_name:
        raise ValueError("AZURE_KEY_VAULT_NAME is not configured")
    return SecretClient(
        vault_url=f"https://{vault_name}.vault.azure.net",
        credential=_get_provisioner_credential(),
    )


def _encode_key(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def _decode_key(value: object, *, secret_name: str) -> bytes:
    try:
        key = base64.b64decode(str(value).encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"Key Vault secret {secret_name} is not valid base64") from exc
    if len(key) != _KEY_BYTES:
        raise ValueError(f"Key Vault secret {secret_name} is not a 32-byte recall search key")
    return key


def _get_or_mint_mock(secret_name: str) -> bytes:
    entry = _MOCK_RECALL_SEARCH_KEY_REGISTRY.get(secret_name)
    if entry is None:
        key = os.urandom(_KEY_BYTES)
        _MOCK_RECALL_SEARCH_KEY_REGISTRY[secret_name] = {"key": key, "deleted": False}
        logger.info("[MOCK] Minted recall search key in Key Vault secret %s", secret_name)
        return key

    if entry.get("deleted"):
        entry["deleted"] = False
        logger.info("[MOCK] Recovered recall search-key secret %s", secret_name)
    return entry["key"]  # type: ignore[return-value]


def _get_or_mint_key_vault(secret_name: str) -> bytes:
    from azure.core.exceptions import ResourceNotFoundError

    client = _secret_client()
    try:
        secret = client.get_secret(secret_name)
    except ResourceNotFoundError:
        try:
            client.get_deleted_secret(secret_name)
        except ResourceNotFoundError:
            key = os.urandom(_KEY_BYTES)
            client.set_secret(secret_name, _encode_key(key))
            logger.info("Minted recall search key in Key Vault secret %s", secret_name)
            return key

        # Match the KEK recovery-window behavior: restore the same secret and
        # therefore the same key. Re-minting here would orphan an existing
        # blind index after an in-grace re-provision.
        client.begin_recover_deleted_secret(secret_name).wait()
        secret = client.get_secret(secret_name)
        logger.info("Recovered recall search-key secret %s", secret_name)

    return _decode_key(secret.value, secret_name=secret_name)


def get_or_mint_recall_search_key(tenant: Tenant) -> bytes:
    """Return this tenant's 32-byte recall blind-index key.

    Cache hit performs no Key Vault I/O. A cold miss is serialized per process,
    loads the existing secret, recovers its soft-deleted value, or mints 32
    random bytes when no secret has ever existed. Errors propagate fail-closed;
    no fallback key is derived.
    """
    tenant_id = str(tenant.id)
    secret_name = _secret_name(tenant)

    key = _CACHE.get(tenant_id)
    if key is not None:
        return key

    with _LOCK:
        key = _CACHE.get(tenant_id)
        if key is not None:
            return key

        key = _get_or_mint_mock(secret_name) if _is_mock() else _get_or_mint_key_vault(secret_name)
        _CACHE[tenant_id] = key
        return key


def begin_delete_recall_search_key(tenant: Tenant) -> None:
    """Soft-delete a tenant's recall key under Key Vault's recovery window.

    The current process evicts its cached copy deliberately. Other processes
    have independent caches, matching the existing KEK/DEK cache limitation.
    This function never purges; irreversible purge remains break-glass work.
    """
    tenant_id = str(tenant.id)
    secret_name = _secret_name(tenant)

    with _LOCK:
        _CACHE.pop(tenant_id, None)

        if _is_mock():
            entry = _MOCK_RECALL_SEARCH_KEY_REGISTRY.get(secret_name)
            if entry is not None:
                entry["deleted"] = True
            logger.info("[MOCK] Soft-deleted recall search-key secret %s", secret_name)
            return

        from azure.core.exceptions import ResourceNotFoundError

        client = _secret_client()
        try:
            client.begin_delete_secret(secret_name).wait()
        except ResourceNotFoundError:
            # Mint-on-demand means an unused tenant legitimately has no secret.
            logger.info("No recall search-key secret to soft-delete for tenant %s", tenant_id)
            return
        logger.info("Soft-deleted recall search-key secret %s (recovery window open)", secret_name)


def blind_index_tokens(key: bytes, lexemes: list[str]) -> list[str]:
    """Return 12-byte HMAC-SHA256 token prefixes as lowercase hex strings.

    Lexemes are lowercased exactly as required by the recall directive. Input
    order and duplicates are preserved deliberately; this helper never dedups.
    """
    return [hmac.new(key, lexeme.lower().encode("utf-8"), hashlib.sha256).digest()[:12].hex() for lexeme in lexemes]
