"""Azure Container Apps SDK client for provisioning OpenClaw instances.

This module wraps the Azure SDK calls. In development/testing, set
AZURE_MOCK=true to skip real Azure calls.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


def _is_mock() -> bool:
    return os.environ.get("AZURE_MOCK", "false").lower() == "true"


def _get_provisioner_credential():
    """Get credential for the provisioning identity (elevated permissions).

    In production, uses the dedicated user-assigned managed identity.
    In local dev, falls back to DefaultAzureCredential (Azure CLI login).
    """
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    client_id = str(getattr(settings, "AZURE_PROVISIONER_CLIENT_ID", "") or "").strip()
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


def get_container_client():
    """Get Azure Container Apps API client."""
    if _is_mock():
        return None

    from azure.mgmt.appcontainers import ContainerAppsAPIClient

    return ContainerAppsAPIClient(_get_provisioner_credential(), settings.AZURE_SUBSCRIPTION_ID)


def get_identity_client():
    """Get Azure Managed Identity client."""
    if _is_mock():
        return None

    from azure.mgmt.msi import ManagedServiceIdentityClient

    return ManagedServiceIdentityClient(_get_provisioner_credential(), settings.AZURE_SUBSCRIPTION_ID)


def _build_container_secret(
    secret_name: str,
    *,
    plain_value: str,
    key_vault_secret_name: str,
    identity_id: str,
) -> dict[str, str]:
    """Build Container Apps secret payload using Key Vault by default."""
    backend = str(getattr(settings, "OPENCLAW_CONTAINER_SECRET_BACKEND", "keyvault") or "keyvault").strip().lower()
    if backend == "keyvault":
        vault_name = str(getattr(settings, "AZURE_KEY_VAULT_NAME", "") or "").strip()
        kv_secret_name = str(key_vault_secret_name or "").strip()
        if vault_name and kv_secret_name and identity_id:
            return {
                "name": secret_name,
                "keyVaultUrl": f"https://{vault_name}.vault.azure.net/secrets/{kv_secret_name}",
                "identity": identity_id,
            }

        logger.warning(
            "Key Vault secret reference disabled for %s due to missing vault/secret/identity; "
            "falling back to inline secret value",
            secret_name,
        )

    return {"name": secret_name, "value": plain_value}


def create_managed_identity(tenant_id: str) -> dict[str, str]:
    """Create a User-Assigned Managed Identity for a tenant.

    Returns dict with 'id', 'client_id', 'principal_id'.
    """
    if _is_mock():
        logger.info("[MOCK] Created managed identity for tenant %s", tenant_id)
        return {
            "id": f"/mock/identity/{tenant_id}",
            "client_id": f"mock-client-{tenant_id}",
            "principal_id": f"mock-principal-{tenant_id}",
        }

    client = get_identity_client()
    identity = client.user_assigned_identities.create_or_update(
        resource_group_name=settings.AZURE_RESOURCE_GROUP,
        resource_name=f"mi-nbhd-{str(tenant_id)[:20]}",
        parameters={
            "location": settings.AZURE_LOCATION,
            "tags": {"tenant_id": str(tenant_id), "service": "nbhd-united"},
        },
    )
    return {
        "id": identity.id,
        "client_id": identity.client_id,
        "principal_id": identity.principal_id,
    }


def get_authorization_client():
    """Get Azure Authorization Management client."""
    if _is_mock():
        return None

    from azure.mgmt.authorization import AuthorizationManagementClient

    return AuthorizationManagementClient(_get_provisioner_credential(), settings.AZURE_SUBSCRIPTION_ID)


def assign_key_vault_role(principal_id: str) -> None:
    """Assign 'Key Vault Secrets User' to identity on the project vault."""
    if _is_mock():
        logger.info("[MOCK] Assigned Key Vault Secrets User to %s", principal_id)
        return

    import uuid

    client = get_authorization_client()

    vault_name = str(getattr(settings, "AZURE_KEY_VAULT_NAME", "") or "").strip()
    if not vault_name:
        raise ValueError("AZURE_KEY_VAULT_NAME is not configured")

    # Well-known Azure built-in role ID for Key Vault Secrets User
    KV_SECRETS_USER_ROLE = "4633458b-17de-408a-b874-0445c86b69e6"

    scope = (
        f"/subscriptions/{settings.AZURE_SUBSCRIPTION_ID}"
        f"/resourceGroups/{settings.AZURE_RESOURCE_GROUP}"
        f"/providers/Microsoft.KeyVault/vaults/{vault_name}"
    )
    role_def_id = f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{KV_SECRETS_USER_ROLE}"

    # Deterministic UUID → idempotent (same identity + role + scope = same assignment name)
    assignment_name = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{principal_id}:{KV_SECRETS_USER_ROLE}:{scope}",
        )
    )

    from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

    try:
        client.role_assignments.create(
            scope=scope,
            role_assignment_name=assignment_name,
            parameters=RoleAssignmentCreateParameters(
                role_definition_id=role_def_id,
                principal_id=principal_id,
                principal_type="ServicePrincipal",
            ),
        )
        logger.info("Assigned KV Secrets User to %s on %s", principal_id, vault_name)
    except Exception as exc:
        if hasattr(exc, "status_code") and exc.status_code == 409:
            logger.info("KV role already assigned to %s (idempotent)", principal_id)
        else:
            raise


def assign_acr_pull_role(principal_id: str) -> None:
    """Assign 'AcrPull' to identity on the project container registry."""
    if _is_mock():
        logger.info("[MOCK] Assigned AcrPull to %s", principal_id)
        return

    import uuid

    client = get_authorization_client()

    acr_server = str(getattr(settings, "AZURE_ACR_SERVER", "") or "").strip()
    if not acr_server:
        raise ValueError("AZURE_ACR_SERVER is not configured")
    # Extract registry name from server FQDN (e.g. "nbhdunited.azurecr.io" -> "nbhdunited")
    acr_name = acr_server.split(".")[0]

    # Well-known Azure built-in role ID for AcrPull
    ACR_PULL_ROLE = "7f951dda-4ed3-4680-a7ca-43fe172d538d"

    scope = (
        f"/subscriptions/{settings.AZURE_SUBSCRIPTION_ID}"
        f"/resourceGroups/{settings.AZURE_RESOURCE_GROUP}"
        f"/providers/Microsoft.ContainerRegistry/registries/{acr_name}"
    )
    role_def_id = f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{ACR_PULL_ROLE}"

    assignment_name = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{principal_id}:{ACR_PULL_ROLE}:{scope}",
        )
    )

    from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

    try:
        client.role_assignments.create(
            scope=scope,
            role_assignment_name=assignment_name,
            parameters=RoleAssignmentCreateParameters(
                role_definition_id=role_def_id,
                principal_id=principal_id,
                principal_type="ServicePrincipal",
            ),
        )
        logger.info("Assigned AcrPull to %s on %s", principal_id, acr_name)
    except Exception as exc:
        if hasattr(exc, "status_code") and exc.status_code == 409:
            logger.info("AcrPull role already assigned to %s (idempotent)", principal_id)
        else:
            raise


def store_tenant_internal_key_in_key_vault(tenant_id: str, plaintext_key: str) -> str:
    """Store a tenant's internal API key in Azure Key Vault.

    Uses naming convention: tenant-<uuid>-internal-key
    Returns the KV secret name.
    """
    secret_name = f"tenant-{tenant_id}-internal-key"

    if _is_mock():
        logger.info("[MOCK] Stored tenant internal key in KV: %s", secret_name)
        return secret_name

    from azure.keyvault.secrets import SecretClient

    vault_name = str(getattr(settings, "AZURE_KEY_VAULT_NAME", "") or "").strip()
    if not vault_name:
        raise ValueError("AZURE_KEY_VAULT_NAME is not configured")

    vault_url = f"https://{vault_name}.vault.azure.net"
    client = SecretClient(vault_url=vault_url, credential=_get_provisioner_credential())
    client.set_secret(secret_name, plaintext_key)

    logger.info("Stored tenant internal key in Key Vault: %s", secret_name)
    return secret_name


def read_key_vault_secret(secret_name: str) -> str | None:
    """Read a secret value from Azure Key Vault.

    Returns the secret value or None if not found / not configured.
    """
    if _is_mock():
        logger.info("[MOCK] Read KV secret: %s", secret_name)
        return None

    from azure.keyvault.secrets import SecretClient

    vault_name = str(getattr(settings, "AZURE_KEY_VAULT_NAME", "") or "").strip()
    if not vault_name:
        logger.warning("AZURE_KEY_VAULT_NAME not configured, cannot read secret %s", secret_name)
        return None

    try:
        vault_url = f"https://{vault_name}.vault.azure.net"
        client = SecretClient(vault_url=vault_url, credential=_get_provisioner_credential())
        secret = client.get_secret(secret_name)
        return secret.value
    except Exception as exc:
        logger.warning("Failed to read KV secret %s: %s", secret_name, exc)
        return None


def get_storage_client():
    """Get Azure Storage Management client."""
    if _is_mock():
        return None

    from azure.mgmt.storage import StorageManagementClient

    return StorageManagementClient(_get_provisioner_credential(), settings.AZURE_SUBSCRIPTION_ID)


def create_tenant_file_share(tenant_id: str) -> dict[str, str]:
    """Create an Azure File Share for a tenant's workspace.

    Returns dict with 'share_name' and 'account_name'.
    """
    share_name = f"ws-{str(tenant_id)[:20]}"
    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()

    if _is_mock():
        logger.info("[MOCK] Created file share %s for tenant %s", share_name, tenant_id)
        return {"share_name": share_name, "account_name": account_name or "mock-storage"}

    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    client = get_storage_client()
    client.file_shares.create(
        resource_group_name=settings.AZURE_RESOURCE_GROUP,
        account_name=account_name,
        share_name=share_name,
        file_share={},
    )
    logger.info("Created file share %s in %s", share_name, account_name)
    return {"share_name": share_name, "account_name": account_name}


def upload_config_to_file_share(tenant_id: str, config_json: str) -> None:
    """Upload openclaw.json to the tenant's Azure File Share.

    Uses atomic write: upload to a temp file first, then rename to the
    target path. This prevents the container from reading a partially-written
    file if it restarts while the upload is in progress.
    """
    share_name = f"ws-{str(tenant_id)[:20]}"

    if _is_mock():
        logger.info("[MOCK] Uploaded config to file share %s", share_name)
        return

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    from azure.storage.fileshare import ShareFileClient

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account_name,
    )
    account_key = keys.keys[0].value
    account_url = f"https://{account_name}.file.core.windows.net"

    # Write to temp file first, then atomic rename — prevents the container
    # from reading a partially-written file during concurrent restarts.
    tmp_client = ShareFileClient(
        account_url=account_url,
        share_name=share_name,
        file_path="openclaw.json.tmp",
        credential=account_key,
    )
    data = config_json.encode("utf-8")
    tmp_client.upload_file(data, length=len(data))

    # Atomic rename: overwrite the target file
    tmp_client.rename_file("openclaw.json", overwrite=True)
    logger.info("Uploaded openclaw.json to file share %s (atomic)", share_name)


def upload_workspace_file(
    tenant_id: str,
    file_path: str,
    content: str,
    *,
    skip_if_exists: bool = False,
) -> None:
    """Upload a workspace file to the tenant's Azure File Share.

    file_path is relative to the workspace root, e.g. 'workspace/AGENTS.md'.

    When ``skip_if_exists`` is True, the upload is a no-op if the file already
    exists on the share. Use this for files the agent owns after first seed
    (SOUL.md, IDENTITY.md) so later config refreshes don't overwrite agent
    edits — this matches the `[ ! -f ]` guards in `runtime/openclaw/entrypoint.sh`.
    """
    share_name = f"ws-{str(tenant_id)[:20]}"

    if _is_mock():
        if skip_if_exists:
            logger.info("[MOCK] Skip-if-exists upload of %s to file share %s", file_path, share_name)
        else:
            logger.info("[MOCK] Uploaded %s to file share %s", file_path, share_name)
        return

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    from azure.storage.fileshare import ShareFileClient

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account_name,
    )
    account_key = keys.keys[0].value
    account_url = f"https://{account_name}.file.core.windows.net"

    if skip_if_exists:
        from azure.core.exceptions import ResourceNotFoundError

        check_client = ShareFileClient(
            account_url=account_url,
            share_name=share_name,
            file_path=file_path,
            credential=account_key,
        )
        try:
            check_client.get_file_properties()
            logger.info(
                "Skipping upload of %s to file share %s (already exists)",
                file_path,
                share_name,
            )
            return
        except ResourceNotFoundError:
            pass  # File missing — fall through and seed it

    from azure.storage.fileshare import ShareDirectoryClient

    # Ensure parent directories exist (e.g. workspace/docs/)
    parts = file_path.split("/")
    for i in range(1, len(parts)):
        dir_path = "/".join(parts[:i])
        dir_client = ShareDirectoryClient(
            account_url=account_url,
            share_name=share_name,
            directory_path=dir_path,
            credential=account_key,
        )
        try:
            dir_client.create_directory()
        except Exception:
            pass  # Already exists

    file_client = ShareFileClient(
        account_url=account_url,
        share_name=share_name,
        file_path=file_path,
        credential=account_key,
    )
    data = content.encode("utf-8")
    file_client.upload_file(data, length=len(data))
    logger.info("Uploaded %s to file share %s", file_path, share_name)


def upload_workspace_file_binary(tenant_id: str, file_path: str, data: bytes) -> None:
    """Upload a binary file to the tenant's Azure File Share.

    file_path is relative to the workspace root, e.g. 'workspace/media/inbound/photo.jpg'.
    Creates intermediate directories if needed.
    """
    share_name = f"ws-{str(tenant_id)[:20]}"

    if _is_mock():
        logger.info("[MOCK] Uploaded binary %s to file share %s", file_path, share_name)
        return

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    from azure.storage.fileshare import ShareDirectoryClient, ShareFileClient

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account_name,
    )
    account_key = keys.keys[0].value

    # Ensure parent directories exist
    parts = file_path.split("/")
    for i in range(1, len(parts)):
        dir_path = "/".join(parts[:i])
        dir_client = ShareDirectoryClient(
            account_url=f"https://{account_name}.file.core.windows.net",
            share_name=share_name,
            directory_path=dir_path,
            credential=account_key,
        )
        try:
            dir_client.create_directory()
        except Exception:
            pass  # Already exists

    file_client = ShareFileClient(
        account_url=f"https://{account_name}.file.core.windows.net",
        share_name=share_name,
        file_path=file_path,
        credential=account_key,
    )
    file_client.upload_file(data, length=len(data))
    logger.info("Uploaded binary %s (%d bytes) to file share %s", file_path, len(data), share_name)


def download_workspace_file(tenant_id: str, file_path: str) -> str | None:
    """Read a workspace file from the tenant's Azure File Share.

    file_path is relative to the workspace root, e.g. 'workspace/USER.md'.
    Returns the file's UTF-8 decoded content, or None if the file does not
    exist. Used by workspace_envelope to merge platform-managed regions into
    files that may already contain agent-written content.
    """
    share_name = f"ws-{str(tenant_id)[:20]}"

    if _is_mock():
        logger.info("[MOCK] Download of %s from file share %s", file_path, share_name)
        return None

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.fileshare import ShareFileClient

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account_name,
    )
    account_key = keys.keys[0].value

    file_client = ShareFileClient(
        account_url=f"https://{account_name}.file.core.windows.net",
        share_name=share_name,
        file_path=file_path,
        credential=account_key,
    )
    try:
        downloader = file_client.download_file()
        data = downloader.readall()
    except ResourceNotFoundError:
        return None
    return data.decode("utf-8", errors="replace")


def register_environment_storage(tenant_id: str) -> None:
    """Register a tenant's file share with the Container Apps Environment."""
    if _is_mock():
        logger.info("[MOCK] Registered environment storage for tenant %s", tenant_id)
        return

    from azure.mgmt.appcontainers.models import (
        AzureFileProperties,
        ManagedEnvironmentStorage,
        ManagedEnvironmentStorageProperties,
    )

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    env_id = str(getattr(settings, "AZURE_CONTAINER_ENV_ID", "") or "").strip()
    if not env_id:
        raise ValueError("AZURE_CONTAINER_ENV_ID is not configured")
    env_name = env_id.split("/")[-1]

    storage_name = f"ws-{str(tenant_id)[:20]}"

    # Get storage account key programmatically
    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account_name,
    )
    account_key = keys.keys[0].value

    container_client = get_container_client()
    container_client.managed_environments_storages.create_or_update(
        resource_group_name=settings.AZURE_RESOURCE_GROUP,
        environment_name=env_name,
        storage_name=storage_name,
        storage_envelope=ManagedEnvironmentStorage(
            properties=ManagedEnvironmentStorageProperties(
                azure_file=AzureFileProperties(
                    account_name=account_name,
                    account_key=account_key,
                    access_mode="ReadWrite",
                    share_name=storage_name,
                ),
            ),
        ),
    )
    logger.info("Registered environment storage %s for tenant %s", storage_name, tenant_id)


def delete_tenant_file_share(tenant_id: str) -> None:
    """Delete a tenant's file share and deregister from the environment."""
    if _is_mock():
        logger.info("[MOCK] Deleted file share for tenant %s", tenant_id)
        return

    storage_name = f"ws-{str(tenant_id)[:20]}"
    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    env_id = str(getattr(settings, "AZURE_CONTAINER_ENV_ID", "") or "").strip()
    env_name = env_id.split("/")[-1] if env_id else ""

    # Deregister from environment first
    if env_name:
        try:
            container_client = get_container_client()
            container_client.managed_environments_storages.delete(
                resource_group_name=settings.AZURE_RESOURCE_GROUP,
                environment_name=env_name,
                storage_name=storage_name,
            )
        except Exception:
            logger.exception("Failed to deregister storage %s from environment", storage_name)

    # Delete the file share
    if account_name:
        try:
            storage_client = get_storage_client()
            storage_client.file_shares.delete(
                resource_group_name=settings.AZURE_RESOURCE_GROUP,
                account_name=account_name,
                share_name=storage_name,
            )
        except Exception:
            logger.exception("Failed to delete file share %s", storage_name)


def delete_managed_identity(tenant_id: str) -> None:
    """Delete a tenant's Managed Identity."""
    if _is_mock():
        logger.info("[MOCK] Deleted managed identity for tenant %s", tenant_id)
        return

    client = get_identity_client()
    try:
        client.user_assigned_identities.delete(
            resource_group_name=settings.AZURE_RESOURCE_GROUP,
            resource_name=f"mi-nbhd-{str(tenant_id)[:20]}",
        )
    except Exception:
        logger.exception("Failed to delete managed identity for %s", tenant_id)


def create_container_app(
    tenant_id: str,
    container_name: str,
    config_json: str,
    identity_id: str,
    identity_client_id: str,
    workspace_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Create an Azure Container App for an OpenClaw instance.

    All containers share a single internal API key from Key Vault
    (AZURE_KV_SECRET_NBHD_INTERNAL_API_KEY). Per-tenant keys were removed
    2026-02-22 — unnecessary given network isolation (containers are
    internal-only).

    Returns dict with 'name' and 'fqdn'.
    """
    if _is_mock():
        fqdn = f"{container_name}.internal.azurecontainerapps.io"
        logger.info("[MOCK] Created container %s at %s", container_name, fqdn)
        return {"name": container_name, "fqdn": fqdn}

    client = get_container_client()
    secrets = [
        _build_container_secret(
            "anthropic-key",
            plain_value=settings.ANTHROPIC_API_KEY,
            key_vault_secret_name=settings.AZURE_KV_SECRET_ANTHROPIC_API_KEY,
            identity_id=identity_id,
        ),
        _build_container_secret(
            "openai-key",
            plain_value=settings.OPENAI_API_KEY,
            key_vault_secret_name=settings.AZURE_KV_SECRET_OPENAI_API_KEY,
            identity_id=identity_id,
        ),
        _build_container_secret(
            "nbhd-internal-api-key",
            plain_value=settings.NBHD_INTERNAL_API_KEY,
            key_vault_secret_name=settings.AZURE_KV_SECRET_NBHD_INTERNAL_API_KEY,
            identity_id=identity_id,
        ),
        _build_container_secret(
            "brave-key",
            plain_value=settings.BRAVE_API_KEY,
            key_vault_secret_name=settings.AZURE_KV_SECRET_BRAVE_API_KEY,
            identity_id=identity_id,
        ),
        _build_container_secret(
            "openrouter-key",
            plain_value=settings.OPENROUTER_API_KEY,
            key_vault_secret_name=settings.AZURE_KV_SECRET_OPENROUTER_API_KEY,
            identity_id=identity_id,
        ),
    ]

    container_app: dict[str, Any] = {
        "location": settings.AZURE_LOCATION,
        "managed_environment_id": settings.AZURE_CONTAINER_ENV_ID,
        "identity": {
            "type": "UserAssigned",
            "user_assigned_identities": {identity_id: {}},
        },
        "properties": {
            "configuration": {
                "registries": [
                    {
                        "server": settings.AZURE_ACR_SERVER,
                        "identity": identity_id,
                    },
                ],
                "ingress": {
                    "external": False,
                    "targetPort": 8080,
                    "transport": "http",
                },
                "secrets": secrets,
            },
            "template": {
                "containers": [
                    {
                        "name": "openclaw",
                        "image": f"{settings.AZURE_ACR_SERVER}/nbhd-openclaw:latest",
                        "resources": {"cpu": 0.5, "memory": "1.0Gi"},
                        "env": [
                            {"name": "ANTHROPIC_API_KEY", "secretRef": "anthropic-key"},
                            {"name": "OPENAI_API_KEY", "secretRef": "openai-key"},
                            {"name": "NBHD_INTERNAL_API_KEY", "secretRef": "nbhd-internal-api-key"},
                            {"name": "OPENCLAW_GATEWAY_TOKEN", "secretRef": "nbhd-internal-api-key"},
                            {"name": "BRAVE_API_KEY", "secretRef": "brave-key"},
                            {"name": "OPENROUTER_API_KEY", "secretRef": "openrouter-key"},
                            {"name": "NBHD_TENANT_ID", "value": str(tenant_id)},
                            {"name": "NBHD_API_BASE_URL", "value": settings.API_BASE_URL},
                            {"name": "OPENCLAW_CONFIG_JSON", "value": config_json},
                            {"name": "AZURE_CLIENT_ID", "value": identity_client_id},
                            # NODE_OPTIONS must be set explicitly here because
                            # Container Apps caches Dockerfile ENV on first
                            # provisioning and never re-reads it on image
                            # updates. The --require loads a targeted handler
                            # for OpenClaw's chmod EPERM on root-owned volumes.
                            {
                                "name": "NODE_OPTIONS",
                                "value": (
                                    "--max-old-space-size=512 "
                                    "--dns-result-order=ipv4first "
                                    "--no-network-family-autoselection "
                                    "--require /opt/nbhd/suppress-chmod-eperm.js"
                                ),
                            },
                            # Disable mDNS/bonjour — useless on Container Apps
                            # and causes intermittent CIAO ANNOUNCEMENT CANCELLED
                            # crashes on startup.
                            {"name": "OPENCLAW_DISABLE_BONJOUR", "value": "1"},
                            *[{"name": k, "value": v} for k, v in (workspace_env or {}).items()],
                        ],
                        "volumeMounts": [
                            {"volumeName": "workspace", "mountPath": "/home/node/.openclaw"},
                            {"volumeName": "sessions-scratch", "mountPath": "/home/node/.openclaw/agents"},
                            # OpenClaw's bundled-channel installer copies files
                            # with modes Azure File Share/SMB doesn't support
                            # (EPERM on `.buildstamp` copy) and leaves a stale
                            # lock dir that wedges across container restarts.
                            # Shadowing this path with EmptyDir keeps the
                            # install on ephemeral local storage.
                            {
                                "volumeName": "plugin-runtime-deps",
                                "mountPath": "/home/node/.openclaw/plugin-runtime-deps",
                            },
                        ],
                    },
                ],
                "volumes": [
                    {
                        "name": "workspace",
                        "storageType": "AzureFile",
                        "storageName": f"ws-{str(tenant_id)[:20]}",
                    },
                    {
                        "name": "sessions-scratch",
                        "storageType": "EmptyDir",
                    },
                    {
                        "name": "plugin-runtime-deps",
                        "storageType": "EmptyDir",
                    },
                ],
                "scale": {
                    "minReplicas": 1,
                    "maxReplicas": 1,
                    "rules": [
                        {"name": "http-trigger", "http": {"metadata": {"concurrentRequests": "1"}}},
                    ],
                },
            },
        },
    }

    result = client.container_apps.begin_create_or_update(
        settings.AZURE_RESOURCE_GROUP,
        container_name,
        container_app,
    ).result()

    # SDK v4 flattens the model — try direct attributes first, then nested .properties
    try:
        fqdn = result.configuration.ingress.fqdn
    except AttributeError:
        fqdn = ""
    return {"name": container_name, "fqdn": fqdn}


def update_container_env_var(
    container_name: str,
    env_name: str,
    env_value: str,
) -> None:
    """Update a single environment variable on an existing Container App."""
    if _is_mock():
        logger.info("[MOCK] Updated %s on %s", env_name, container_name)
        return

    client = get_container_client()
    app = client.container_apps.get(
        settings.AZURE_RESOURCE_GROUP,
        container_name,
    )

    for container in app.template.containers:
        env_list = container.env or []
        for env_entry in env_list:
            if env_entry.name == env_name:
                env_entry.value = env_value
                break
        else:
            env_list.append({"name": env_name, "value": env_value})
        container.env = env_list

    client.container_apps.begin_create_or_update(
        settings.AZURE_RESOURCE_GROUP,
        container_name,
        app,
    ).result()
    logger.info("Updated env var %s on container %s", env_name, container_name)


def restart_container_app(container_name: str) -> None:
    """Restart the active revision of a Container App."""
    if _is_mock():
        logger.info("[MOCK] Restarted container %s", container_name)
        return

    client = get_container_client()

    app = client.container_apps.get(
        settings.AZURE_RESOURCE_GROUP,
        container_name,
    )

    import hashlib
    import time

    template = app.template
    # Use a short, deterministic restart suffix so revision DNS labels stay < 64 chars.
    template.revision_suffix = f"r{hashlib.sha256(f'{int(time.time())}'.encode()).hexdigest()[:6]}"

    client.container_apps.begin_create_or_update(
        settings.AZURE_RESOURCE_GROUP,
        container_name,
        app,
    ).result()
    logger.info("Restarted container app %s", container_name)


def _entry_name(entry: Any) -> str | None:
    """Extract `.name` from a Container Apps SDK Secret/EnvVar typed
    object or a plain dict (we mix both in our spec lists)."""
    if isinstance(entry, dict):
        return entry.get("name")
    return getattr(entry, "name", None)


def apply_byo_credentials_to_container(tenant: Any) -> None:
    """Reconcile the tenant container's BYO secret + env bindings, then
    create a new revision so the Container Apps runtime picks up any
    changed Key Vault references.

    Per microsoft/azure-container-apps#856 a plain restart keeps cached
    KV values; only revision creation triggers a re-fetch. Each call
    here mutates `template.revision_suffix` (causing a new revision)
    even when the cred state hasn't changed — that's intentional: the
    user just pasted/disconnected and expects the change to take effect.

    Phase 1 reconciliation, for Anthropic CLI subscription only:
      - Active cred → add `CLAUDE_CODE_OAUTH_TOKEN` env (KV-backed)
        AND remove the `ANTHROPIC_API_KEY` env binding (auth-precedence
        shadowing — Anthropic's CLI ranks `ANTHROPIC_API_KEY` ABOVE
        `CLAUDE_CODE_OAUTH_TOKEN`, so the platform key would win and
        bill against API credits instead of the user's subscription).
      - No active cred → ensure `ANTHROPIC_API_KEY` is restored
        (re-bound to the existing `anthropic-key` secret) and
        `CLAUDE_CODE_OAUTH_TOKEN` is removed.

    Idempotent — safe to call repeatedly. No-op for tenants without a
    container_id.
    """
    if _is_mock():
        logger.info("[MOCK] Applied BYO credentials for tenant=%s", tenant.id)
        return

    if not tenant.container_id:
        logger.warning(
            "apply_byo_credentials_to_container skipped: tenant=%s has no container_id",
            tenant.id,
        )
        return

    # Late import to avoid circular import (byo_models -> orchestrator).
    from apps.byo_models.models import BYOCredential

    cred = (
        tenant.byo_credentials.filter(
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
        )
        .exclude(status=BYOCredential.Status.ERROR)
        .first()
    )

    client = get_container_client()
    app = client.container_apps.get(
        settings.AZURE_RESOURCE_GROUP,
        tenant.container_id,
    )

    BYO_SECRET = "claude-code-oauth-token"
    BYO_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
    PLATFORM_ENV = "ANTHROPIC_API_KEY"

    # Reconcile secrets list — drop any stale BYO entry, optionally re-add.
    secrets = [s for s in (app.configuration.secrets or []) if _entry_name(s) != BYO_SECRET]
    if cred:
        secrets.append(
            _build_container_secret(
                BYO_SECRET,
                plain_value="",
                key_vault_secret_name=cred.key_vault_secret_name,
                identity_id=tenant.managed_identity_id,
            )
        )
    app.configuration.secrets = secrets

    # Reconcile env on the openclaw container.
    for container in app.template.containers:
        if container.name != "openclaw":
            continue
        env_list = [e for e in (container.env or []) if _entry_name(e) not in (BYO_ENV, PLATFORM_ENV)]
        if cred:
            env_list.append({"name": BYO_ENV, "secretRef": BYO_SECRET})
        else:
            env_list.append({"name": PLATFORM_ENV, "secretRef": "anthropic-key"})
        container.env = env_list
        break

    import hashlib
    import time

    app.template.revision_suffix = f"b{hashlib.sha256(f'byo-{int(time.time_ns())}'.encode()).hexdigest()[:6]}"

    client.container_apps.begin_create_or_update(
        settings.AZURE_RESOURCE_GROUP,
        tenant.container_id,
        app,
    ).result()
    logger.info(
        "Applied BYO credentials for tenant=%s container=%s (cli_active=%s)",
        tenant.id,
        tenant.container_id,
        cred is not None,
    )


_PLUGIN_RUNTIME_DEPS_VOLUME = "plugin-runtime-deps"
_PLUGIN_RUNTIME_DEPS_PATH = "/home/node/.openclaw/plugin-runtime-deps"


def _ensure_plugin_runtime_deps_in_template(app) -> bool:
    """Mutate a Container App template in place to include the
    plugin-runtime-deps EmptyDir mount on the openclaw container.

    Returns True if the template was modified, False if the mount was
    already present. Caller is responsible for persisting the change.
    """
    from azure.mgmt.appcontainers.models import Volume, VolumeMount

    template = app.template
    volumes = list(template.volumes or [])
    volume_names = {v.name for v in volumes}

    modified = False

    if _PLUGIN_RUNTIME_DEPS_VOLUME not in volume_names:
        volumes.append(Volume(name=_PLUGIN_RUNTIME_DEPS_VOLUME, storage_type="EmptyDir"))
        template.volumes = volumes
        modified = True

    for container in template.containers:
        if container.name != "openclaw":
            continue
        mounts = list(container.volume_mounts or [])
        if not any(m.volume_name == _PLUGIN_RUNTIME_DEPS_VOLUME for m in mounts):
            mounts.append(
                VolumeMount(
                    volume_name=_PLUGIN_RUNTIME_DEPS_VOLUME,
                    mount_path=_PLUGIN_RUNTIME_DEPS_PATH,
                )
            )
            container.volume_mounts = mounts
            modified = True
        break

    return modified


def ensure_plugin_runtime_deps_mount(container_name: str) -> bool:
    """Idempotently add the plugin-runtime-deps EmptyDir mount to an
    existing Container App.

    OpenClaw's bundled-channel installer hits EPERM when copying
    `.buildstamp` onto an Azure File Share (SMB doesn't support the
    file modes Node uses) and leaves a stale runtime-deps lock that
    wedges across container restarts. Mounting EmptyDir at the install
    target keeps the install on ephemeral local storage.

    Returns True if a new revision was created, False if the mount was
    already present (no-op).
    """
    if _is_mock():
        logger.info("[MOCK] Ensured plugin-runtime-deps mount on %s", container_name)
        return False

    client = get_container_client()
    app = client.container_apps.get(
        settings.AZURE_RESOURCE_GROUP,
        container_name,
    )

    if not _ensure_plugin_runtime_deps_in_template(app):
        return False

    client.container_apps.begin_create_or_update(
        settings.AZURE_RESOURCE_GROUP,
        container_name,
        app,
    ).result()
    logger.info("Added plugin-runtime-deps EmptyDir mount to %s", container_name)
    return True


def update_container_image(container_name: str, image: str) -> None:
    """Update the container image of an existing Container App.

    This triggers a new revision, effectively restarting the container.
    Also ensures the plugin-runtime-deps EmptyDir mount is present so
    image bumps roll out the volume fix in the same revision.
    """
    import hashlib

    if _is_mock():
        logger.info("[MOCK] Updated image to %s on %s", image, container_name)
        return

    client = get_container_client()
    app = client.container_apps.get(
        settings.AZURE_RESOURCE_GROUP,
        container_name,
    )

    for container in app.template.containers:
        if container.name == "openclaw":
            container.image = image
            break

    _ensure_plugin_runtime_deps_in_template(app)

    # Generate a unique revision suffix from the image tag to avoid
    # "revision with suffix already exists" errors.
    # Azure limits suffix to 64 chars and requires lowercase alphanumeric + hyphens.
    tag = image.rsplit(":", 1)[-1] if ":" in image else "latest"
    suffix = hashlib.sha256(tag.encode()).hexdigest()[:6]
    app.template.revision_suffix = f"u{suffix}"

    client.container_apps.begin_create_or_update(
        settings.AZURE_RESOURCE_GROUP,
        container_name,
        app,
    ).result()
    logger.info("Updated image to %s on %s (revision suffix: u%s)", image, container_name, suffix)


def scale_container_app(container_name: str, *, min_replicas: int, max_replicas: int) -> None:
    """Scale an Azure Container App by activating/deactivating revisions.

    Azure Consumption plan doesn't support minReplicas=0 via scale config,
    so we use revision deactivation to hibernate (stop all replicas, zero cost)
    and revision activation to wake up.

    Use min=0, max=0 to hibernate (deactivate all active revisions).
    Use min=1, max=1 to wake (activate the latest revision).
    """
    if _is_mock():
        logger.info("[MOCK] Scaled %s to min=%d max=%d", container_name, min_replicas, max_replicas)
        return

    client = get_container_client()
    rg = settings.AZURE_RESOURCE_GROUP

    if min_replicas == 0 and max_replicas == 0:
        # Hibernate: deactivate all active revisions
        hibernate_container_app(container_name)
    else:
        # Wake: activate the latest revision
        wake_container_app(container_name)


def hibernate_container_app(container_name: str) -> None:
    """Hibernate a container by deactivating all active revisions.

    The container app and its config/data are preserved.
    Zero replicas run = zero compute cost.
    """
    if _is_mock():
        logger.info("[MOCK] Hibernated %s", container_name)
        return

    client = get_container_client()
    rg = settings.AZURE_RESOURCE_GROUP

    revisions = list(client.container_apps_revisions.list_revisions(rg, container_name))
    active_revs = [r for r in revisions if r.active]

    for rev in active_revs:
        client.container_apps_revisions.deactivate_revision(rg, container_name, rev.name)

    logger.info("Hibernated %s — deactivated %d revision(s)", container_name, len(active_revs))


def wake_container_app(container_name: str) -> None:
    """Wake a hibernated container by activating the latest revision.

    Finds the most recently created revision and activates it.
    Container starts within ~30 seconds.
    """
    if _is_mock():
        logger.info("[MOCK] Woke %s", container_name)
        return

    client = get_container_client()
    rg = settings.AZURE_RESOURCE_GROUP

    revisions = list(client.container_apps_revisions.list_revisions(rg, container_name))
    if not revisions:
        raise RuntimeError(f"No revisions found for {container_name}")

    # Find the latest revision
    latest = max(revisions, key=lambda r: r.created_time)

    if latest.active:
        logger.info("Wake %s — latest revision %s already active", container_name, latest.name)
        return

    client.container_apps_revisions.activate_revision(rg, container_name, latest.name)
    logger.info("Woke %s — activated revision %s", container_name, latest.name)


def delete_container_app(container_name: str) -> None:
    """Delete an Azure Container App."""
    if _is_mock():
        logger.info("[MOCK] Deleted container %s", container_name)
        return

    client = get_container_client()
    try:
        client.container_apps.begin_delete(
            settings.AZURE_RESOURCE_GROUP,
            container_name,
        ).result()
    except Exception:
        logger.exception("Failed to delete container %s", container_name)
        raise
