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


def get_container_client():
    """Get Azure Container Apps API client."""
    if _is_mock():
        return None

    from azure.identity import DefaultAzureCredential
    from azure.mgmt.appcontainers import ContainerAppsAPIClient

    credential = DefaultAzureCredential()
    return ContainerAppsAPIClient(credential, settings.AZURE_SUBSCRIPTION_ID)


def get_identity_client():
    """Get Azure Managed Identity client."""
    if _is_mock():
        return None

    from azure.identity import DefaultAzureCredential
    from azure.mgmt.msi import ManagedServiceIdentityClient

    credential = DefaultAzureCredential()
    return ManagedServiceIdentityClient(credential, settings.AZURE_SUBSCRIPTION_ID)


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
) -> dict[str, str]:
    """Create an Azure Container App for an OpenClaw instance.

    Returns dict with 'name' and 'fqdn'.
    """
    if _is_mock():
        fqdn = f"{container_name}.internal.azurecontainerapps.io"
        logger.info("[MOCK] Created container %s at %s", container_name, fqdn)
        return {"name": container_name, "fqdn": fqdn}

    client = get_container_client()

    container_app: dict[str, Any] = {
        "location": settings.AZURE_LOCATION,
        "managed_environment_id": settings.AZURE_CONTAINER_ENV_ID,
        "identity": {
            "type": "UserAssigned",
            "user_assigned_identities": {identity_id: {}},
        },
        "properties": {
            "configuration": {
                "ingress": {
                    "external": False,
                    "targetPort": 18789,
                    "transport": "http",
                },
                "secrets": [
                    {"name": "anthropic-key", "value": settings.ANTHROPIC_API_KEY},
                    {"name": "telegram-token", "value": settings.TELEGRAM_BOT_TOKEN},
                    {"name": "nbhd-internal-api-key", "value": settings.NBHD_INTERNAL_API_KEY},
                ],
            },
            "template": {
                "containers": [
                    {
                        "name": "openclaw",
                        "image": f"{settings.AZURE_ACR_SERVER}/nbhd-openclaw:latest",
                        "resources": {"cpu": 0.25, "memory": "0.5Gi"},
                        "env": [
                            {"name": "ANTHROPIC_API_KEY", "secretRef": "anthropic-key"},
                            {"name": "TELEGRAM_BOT_TOKEN", "secretRef": "telegram-token"},
                            {"name": "NBHD_INTERNAL_API_KEY", "secretRef": "nbhd-internal-api-key"},
                            {"name": "NBHD_TENANT_ID", "value": str(tenant_id)},
                            {"name": "NBHD_API_BASE_URL", "value": settings.API_BASE_URL},
                            {"name": "OPENCLAW_CONFIG_JSON", "value": config_json},
                            {"name": "AZURE_CLIENT_ID", "value": identity_client_id},
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

    fqdn = result.properties.configuration.ingress.fqdn if result.properties else ""
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
