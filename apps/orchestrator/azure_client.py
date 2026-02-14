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
    backend = str(
        getattr(settings, "OPENCLAW_CONTAINER_SECRET_BACKEND", "keyvault") or "keyvault"
    ).strip().lower()
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
    assignment_name = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"{principal_id}:{KV_SECRETS_USER_ROLE}:{scope}",
    ))

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

    assignment_name = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"{principal_id}:{ACR_PULL_ROLE}:{scope}",
    ))

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
        settings.AZURE_RESOURCE_GROUP, account_name,
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
    internal_api_key_kv_secret: str = "",
    workspace_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Create an Azure Container App for an OpenClaw instance.

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
            "telegram-token",
            plain_value=settings.TELEGRAM_BOT_TOKEN,
            key_vault_secret_name=settings.AZURE_KV_SECRET_TELEGRAM_BOT_TOKEN,
            identity_id=identity_id,
        ),
        _build_container_secret(
            "nbhd-internal-api-key",
            plain_value=settings.NBHD_INTERNAL_API_KEY,
            key_vault_secret_name=internal_api_key_kv_secret or settings.AZURE_KV_SECRET_NBHD_INTERNAL_API_KEY,
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
                    "targetPort": 18789,
                    "transport": "http",
                },
                "secrets": secrets,
            },
            "template": {
                "containers": [
                    {
                        "name": "openclaw",
                        "image": f"{settings.AZURE_ACR_SERVER}/nbhd-openclaw:latest",
                        "resources": {"cpu": 0.5, "memory": "1Gi"},
                        "env": [
                            {"name": "ANTHROPIC_API_KEY", "secretRef": "anthropic-key"},
                            {"name": "TELEGRAM_BOT_TOKEN", "secretRef": "telegram-token"},
                            {"name": "NBHD_INTERNAL_API_KEY", "secretRef": "nbhd-internal-api-key"},
                            {"name": "NBHD_TENANT_ID", "value": str(tenant_id)},
                            {"name": "NBHD_API_BASE_URL", "value": settings.API_BASE_URL},
                            {"name": "OPENCLAW_CONFIG_JSON", "value": config_json},
                            {"name": "AZURE_CLIENT_ID", "value": identity_client_id},
                            *[
                                {"name": k, "value": v}
                                for k, v in (workspace_env or {}).items()
                            ],
                        ],
                        "volumeMounts": [
                            {"volumeName": "workspace", "mountPath": "/home/node/.openclaw"},
                        ],
                    },
                ],
                "volumes": [
                    {
                        "name": "workspace",
                        "storageType": "AzureFile",
                        "storageName": f"ws-{str(tenant_id)[:20]}",
                    },
                ],
                "scale": {
                    "minReplicas": 0,
                    "maxReplicas": 1,
                    "rules": [
                        {"name": "http-trigger", "http": {"metadata": {"concurrentRequests": "1"}}},
                    ],
                },
            },
        },
    }

    result = client.container_apps.begin_create_or_update(
        settings.AZURE_RESOURCE_GROUP, container_name, container_app,
    ).result()

    # SDK v4 flattens the model — try direct attributes first, then nested .properties
    try:
        fqdn = result.configuration.ingress.fqdn
    except AttributeError:
        fqdn = ""
    return {"name": container_name, "fqdn": fqdn}


def delete_container_app(container_name: str) -> None:
    """Delete an Azure Container App."""
    if _is_mock():
        logger.info("[MOCK] Deleted container %s", container_name)
        return

    client = get_container_client()
    try:
        client.container_apps.begin_delete(
            settings.AZURE_RESOURCE_GROUP, container_name,
        ).result()
    except Exception:
        logger.exception("Failed to delete container %s", container_name)
