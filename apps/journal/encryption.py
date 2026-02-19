"""Per-tenant journal document encryption helpers."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import time
from datetime import UTC, datetime
from threading import Lock
from typing import Final, Optional
from uuid import UUID

from django.conf import settings

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS: Final[int] = 300  # 5 minutes
_KEY_BACKUP_CONTAINER: Final[str] = "key-backups"
_KEY_NAME_TEMPLATE: Final[str] = "tenant-{tenant_id}-journal-key"

# tenant_id -> (key, expires_at_epoch_seconds)
_tenant_key_cache: dict[str, tuple[bytes, float]] = {}
_tenant_key_cache_lock = Lock()


def _is_mock() -> bool:
    return str(getattr(settings, "AZURE_MOCK", "false") or "").lower() == "true"


def _as_uuid(value: UUID | str) -> str:
    return str(UUID(str(value)))


def _get_secret_client():
    from azure.keyvault.secrets import SecretClient
    from azure.identity import DefaultAzureCredential

    vault_name = str(getattr(settings, "AZURE_KEY_VAULT_NAME", "") or "").strip()
    if not vault_name:
        raise ValueError("AZURE_KEY_VAULT_NAME is not configured")

    vault_url = f"https://{vault_name}.vault.azure.net"
    credential = DefaultAzureCredential()
    return SecretClient(vault_url=vault_url, credential=credential)


def _decode_base64(raw: str, field: str) -> bytes:
    try:
        return base64.b64decode(raw.encode("utf-8"), validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError(f"invalid {field}: must be base64-encoded") from exc


def _encode_payload(nonce: bytes, ciphertext: bytes, tag: bytes) -> str:
    return ":".join((
        base64.b64encode(nonce).decode("utf-8"),
        base64.b64encode(ciphertext).decode("utf-8"),
        base64.b64encode(tag).decode("utf-8"),
    ))


def _decode_payload(payload: str) -> tuple[bytes, bytes, bytes]:
    parts = payload.split(":")
    if len(parts) != 3:
        raise ValueError("ciphertext must be nonce:ciphertext:tag")

    nonce = _decode_base64(parts[0], field="nonce")
    ciphertext = _decode_base64(parts[1], field="ciphertext")
    tag = _decode_base64(parts[2], field="tag")

    if len(nonce) != 12:
        raise ValueError("invalid nonce length: expected 12 bytes")
    if len(tag) != 16:
        raise ValueError("invalid tag length: expected 16 bytes")

    return nonce, ciphertext, tag


def _is_encryption_format(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        nonce, _, tag = _decode_payload(value)
    except Exception:
        return False
    return len(nonce) == 12 and len(tag) == 16


def _cached_tenant_key(tenant_id: str) -> Optional[bytes]:
    now = time.time()
    with _tenant_key_cache_lock:
        cached = _tenant_key_cache.get(tenant_id)
        if not cached:
            return None
        key, expires_at = cached
        if expires_at < now:
            _tenant_key_cache.pop(tenant_id, None)
            return None
        return key


def _set_cached_tenant_key(tenant_id: str, key: bytes) -> None:
    with _tenant_key_cache_lock:
        _tenant_key_cache[tenant_id] = (key, time.time() + CACHE_TTL_SECONDS)


def _blob_service_client():
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    account_url = f"https://{account_name}.blob.core.windows.net"
    return BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())


def _check_key_vault_soft_delete_and_purge_protection(vault_name: str) -> None:
    """Best-effort check for soft-delete / purge protection.

    This is non-fatal: if the management SDK is unavailable or the caller
    lacks the required access, the app keeps running and logs what it found.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.keyvault import KeyVaultManagementClient

        subscription_id = str(getattr(settings, "AZURE_SUBSCRIPTION_ID", "") or "").strip()
        if not subscription_id:
            logger.warning("AZURE_SUBSCRIPTION_ID is not configured; skipping key vault protection check")
            return

        mgmt_client = KeyVaultManagementClient(DefaultAzureCredential(), subscription_id)
        resource_group = str(getattr(settings, "AZURE_RESOURCE_GROUP", "") or "").strip()
        vault = mgmt_client.vaults.get(resource_group, vault_name)
        props = vault.properties

        if not getattr(props, "enable_soft_delete", False):
            logger.warning(
                "Key Vault %s soft-delete is disabled; enable_soft_delete for production safety",
                vault_name,
            )
        if not getattr(props, "enable_purge_protection", False):
            logger.warning(
                "Key Vault %s purge protection is disabled; enable_purge_protection for production safety",
                vault_name,
            )
    except ImportError:
        logger.debug("azure-mgmt-keyvault is not installed; cannot verify vault soft-delete/purge protection")
    except Exception as exc:  # pragma: no cover - permission/network dependent
        logger.debug("Could not verify vault soft-delete/purge settings for %s: %s", vault_name, exc)


def encrypt(plaintext: str, key: bytes) -> str:
    """Encrypt a UTF-8 plaintext with AES-256-GCM and return nonce:ciphertext:tag."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise ValueError("key must be 32-byte AES-256 key")

    if plaintext is None:
        plaintext = ""

    aesgcm = AESGCM(bytes(key))
    nonce = os.urandom(12)
    encrypted = aesgcm.encrypt(nonce, str(plaintext).encode("utf-8"), None)

    if len(encrypted) < 16:
        raise RuntimeError("unexpected encrypted payload length")

    ciphertext = encrypted[:-16]
    tag = encrypted[-16:]
    return _encode_payload(nonce, ciphertext, tag)


def decrypt(ciphertext: str, key: bytes) -> str:
    """Decrypt value produced by :func:`encrypt`. Returns UTF-8 plaintext."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise ValueError("key must be 32-byte AES-256 key")
    if not ciphertext:
        return ""

    nonce, ct, tag = _decode_payload(ciphertext)
    aesgcm = AESGCM(bytes(key))
    try:
        plaintext = aesgcm.decrypt(nonce, ct + tag, None)
    except Exception as exc:
        raise ValueError("failed to decrypt: invalid ciphertext") from exc
    return plaintext.decode("utf-8")


def get_tenant_key(tenant_id: UUID) -> bytes:
    """Return raw key bytes for a tenant from Key Vault (cached 5 minutes)."""
    tenant_id_str = _as_uuid(tenant_id)
    cached = _cached_tenant_key(tenant_id_str)
    if cached is not None:
        return cached

    from apps.tenants.models import Tenant

    tenant = Tenant.objects.filter(id=tenant_id_str).first()
    if tenant is None:
        raise ValueError(f"tenant {tenant_id_str} not found")

    secret_name = str(getattr(tenant, "encryption_key_ref", "") or "").strip()
    if not secret_name:
        raise ValueError(
            f"tenant {tenant_id_str} has no encryption_key_ref configured",
        )

    if _is_mock():
        # In mock mode, avoid Key Vault dependency; tests that rely on this should
        # either use monkeypatches or provide local fixtures.
        logger.info("[MOCK] Returning deterministic placeholder key for tenant %s", tenant_id_str)
        key = b"\x00" * 32
        _set_cached_tenant_key(tenant_id_str, key)
        return key

    try:
        vault_name = str(getattr(settings, "AZURE_KEY_VAULT_NAME", "") or "").strip()
        if not vault_name:
            raise ValueError("AZURE_KEY_VAULT_NAME is not configured")

        _check_key_vault_soft_delete_and_purge_protection(vault_name)

        client = _get_secret_client()
        secret = client.get_secret(secret_name)
        raw_key = secret.value
        if raw_key is None:
            raise ValueError(f"Key Vault secret {secret_name} has no value")

        key = _decode_base64(raw_key, field="secret key")
    except Exception as exc:
        raise ValueError(f"failed to load tenant journal key for {tenant_id_str}") from exc

    if len(key) != 32:
        raise ValueError(f"tenant {tenant_id_str} key must be 32 bytes, got {len(key)}")

    _set_cached_tenant_key(tenant_id_str, key)
    return key


def create_tenant_key(tenant_id: UUID) -> str:
    """Generate and store a new per-tenant 32-byte journal key.

    Returns the Key Vault secret name.
    """
    tenant_id_str = _as_uuid(tenant_id)
    secret_name = _KEY_NAME_TEMPLATE.format(tenant_id=tenant_id_str)

    if _is_mock():
        logger.info("[MOCK] Created tenant journal key in KV: %s", secret_name)
        return secret_name

    raw_key = base64.b64encode(os.urandom(32)).decode("utf-8")
    client = _get_secret_client()

    vault_name = str(getattr(settings, "AZURE_KEY_VAULT_NAME", "") or "").strip()
    if vault_name:
        _check_key_vault_soft_delete_and_purge_protection(vault_name)

    client.set_secret(secret_name, raw_key)
    # invalidate cache, caller may refresh immediately
    with _tenant_key_cache_lock:
        _tenant_key_cache.pop(tenant_id_str, None)

    logger.info("Stored tenant journal key in Key Vault: %s", secret_name)
    return secret_name


def backup_tenant_key(tenant_id: UUID) -> str:
    """Backup the tenant's Key Vault secret to blob storage.

    Returns the blob path used.
    """
    tenant_id_str = _as_uuid(tenant_id)
    from apps.tenants.models import Tenant

    tenant = Tenant.objects.filter(id=tenant_id_str).first()
    if tenant is None:
        raise ValueError(f"tenant {tenant_id_str} not found")

    secret_name = str(getattr(tenant, "encryption_key_ref", "") or "").strip()
    if not secret_name:
        raise ValueError(f"tenant {tenant_id_str} has no encryption_key_ref configured")

    if _is_mock():
        logger.info("[MOCK] Would backup tenant key %s for tenant %s", secret_name, tenant_id_str)
        return f"{_KEY_BACKUP_CONTAINER}/{tenant_id_str}/{secret_name}.bin"

    client = _get_secret_client()
    backup = client.backup_secret(secret_name)
    backup_value = getattr(backup, "value", None)
    if not backup_value:
        raise RuntimeError(f"Key Vault secret backup for {secret_name} is empty")

    if isinstance(backup_value, str):
        backup_value = backup_value.encode("utf-8")

    blob_service = _blob_service_client()
    container = blob_service.get_container_client(_KEY_BACKUP_CONTAINER)
    try:
        container.create_container()
    except Exception:
        # Non-fatal: container may already exist or race; upload will confirm/fail
        logger.debug("key-backups container already exists or could not be created explicitly")

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    blob_name = f"{tenant_id_str}/{secret_name}-{timestamp}.bin"
    container.upload_blob(name=blob_name, data=backup_value, overwrite=True)

    logger.info("Backed up tenant key %s to blob %s", secret_name, blob_name)
    return blob_name