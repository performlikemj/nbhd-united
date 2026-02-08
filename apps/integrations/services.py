"""Integration services — OAuth flows and Key Vault writes."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from django.conf import settings

from apps.tenants.models import Tenant
from .models import Integration

logger = logging.getLogger(__name__)

# OAuth provider configs
OAUTH_PROVIDERS: dict[str, dict[str, Any]] = {
    "gmail": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "provider_group": "google",
    },
    "google-calendar": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
        "provider_group": "google",
    },
}


def get_key_vault_secret_name(tenant: Tenant, provider: str) -> str:
    """Get the Key Vault secret name for a tenant's integration."""
    return f"{tenant.key_vault_prefix}-{provider}-token"


def store_tokens_in_key_vault(
    tenant: Tenant,
    provider: str,
    tokens: dict[str, Any],
) -> str:
    """Store OAuth tokens in Azure Key Vault.

    Returns the secret name.
    """
    secret_name = get_key_vault_secret_name(tenant, provider)
    secret_value = json.dumps(tokens)

    if os.environ.get("AZURE_MOCK", "false").lower() == "true":
        logger.info("[MOCK] Stored tokens in Key Vault: %s", secret_name)
        return secret_name

    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    credential = DefaultAzureCredential()
    vault_url = f"https://{settings.AZURE_KEY_VAULT_NAME}.vault.azure.net"
    client = SecretClient(vault_url=vault_url, credential=credential)
    client.set_secret(secret_name, secret_value)

    logger.info("Stored tokens in Key Vault: %s", secret_name)
    return secret_name


def delete_tokens_from_key_vault(tenant: Tenant, provider: str) -> None:
    """Delete OAuth tokens from Azure Key Vault."""
    secret_name = get_key_vault_secret_name(tenant, provider)

    if os.environ.get("AZURE_MOCK", "false").lower() == "true":
        logger.info("[MOCK] Deleted tokens from Key Vault: %s", secret_name)
        return

    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    credential = DefaultAzureCredential()
    vault_url = f"https://{settings.AZURE_KEY_VAULT_NAME}.vault.azure.net"
    client = SecretClient(vault_url=vault_url, credential=credential)

    try:
        client.begin_delete_secret(secret_name).result()
    except Exception:
        logger.exception("Failed to delete Key Vault secret %s", secret_name)


def connect_integration(
    tenant: Tenant,
    provider: str,
    tokens: dict[str, Any],
    provider_email: str = "",
    scopes: list[str] | None = None,
) -> Integration:
    """Connect an integration — store tokens and create/update record."""
    secret_name = store_tokens_in_key_vault(tenant, provider, tokens)

    integration, created = Integration.objects.update_or_create(
        tenant=tenant,
        provider=provider,
        defaults={
            "status": Integration.Status.ACTIVE,
            "scopes": scopes or [],
            "provider_email": provider_email,
            "key_vault_secret_name": secret_name,
        },
    )

    logger.info(
        "%s integration %s for tenant %s",
        "Created" if created else "Updated",
        provider,
        tenant.id,
    )
    return integration


def disconnect_integration(tenant: Tenant, provider: str) -> None:
    """Disconnect an integration — delete tokens and mark revoked."""
    delete_tokens_from_key_vault(tenant, provider)

    Integration.objects.filter(tenant=tenant, provider=provider).update(
        status=Integration.Status.REVOKED,
    )

    logger.info("Disconnected %s for tenant %s", provider, tenant.id)
