"""Integration services — OAuth flows, Key Vault writes, and Composio managed auth."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone

from apps.tenants.models import Tenant

from .models import Integration

logger = logging.getLogger(__name__)
_MOCK_KEY_VAULT_STORE: dict[str, str] = {}
ON_DEMAND_REFRESH_LEEWAY_SECONDS = 120

# ---------------------------------------------------------------------------
# Composio managed-auth helpers
# ---------------------------------------------------------------------------

COMPOSIO_MANAGED_PROVIDERS: set[str] = {"reddit"}

_composio_client = None


def is_composio_provider(provider: str) -> bool:
    """Return True if provider auth is managed through Composio."""
    return provider in COMPOSIO_MANAGED_PROVIDERS and bool(getattr(settings, "COMPOSIO_API_KEY", ""))


def _get_composio_client():
    """Lazy singleton for the Composio client."""
    global _composio_client
    if _composio_client is None:
        from composio import Composio

        api_key = settings.COMPOSIO_API_KEY
        if not api_key:
            raise IntegrationProviderConfigError("COMPOSIO_API_KEY not configured")
        _composio_client = Composio(api_key=api_key)

        # Ensure credential masking is disabled so connected_accounts.get()
        # returns real access tokens instead of "ya29..."-style placeholders.
        try:
            _composio_client._client.project.config.update(
                mask_secret_keys_in_connected_account=False,
            )
        except Exception:
            logger.warning(
                "Failed to disable Composio credential masking; tokens may be masked",
                exc_info=True,
            )

    return _composio_client


def _get_composio_auth_config_id(provider: str) -> str:
    """Map a provider name to its Composio auth-config ID from settings."""
    mapping = {
        "reddit": getattr(settings, "COMPOSIO_REDDIT_AUTH_CONFIG_ID", ""),
    }
    config_id = mapping.get(provider, "")
    if not config_id:
        raise IntegrationProviderConfigError(f"No Composio auth_config_id configured for provider={provider}")
    return config_id


def initiate_composio_connection(
    tenant: Tenant,
    provider: str,
    callback_url: str,
) -> tuple[str, str]:
    """Start a Composio OAuth connection.

    Returns (redirect_url, connection_request_id).
    """
    client = _get_composio_client()
    auth_config_id = _get_composio_auth_config_id(provider)
    allow_multiple = getattr(settings, "COMPOSIO_ALLOW_MULTIPLE_ACCOUNTS", True)

    # One connection per tenant per provider
    user_id = f"tenant-{tenant.id}"

    connection_request = client.connected_accounts.initiate(
        user_id=user_id,
        auth_config_id=auth_config_id,
        callback_url=callback_url,
        allow_multiple=allow_multiple,
    )

    return connection_request.redirect_url, connection_request.id


def complete_composio_connection(
    tenant: Tenant,
    provider: str,
    connection_request_id: str,
) -> Integration:
    """Wait for a Composio connection to become active, then persist."""
    client = _get_composio_client()

    connected_account = client.connected_accounts.wait_for_connection(
        id=connection_request_id,
        timeout=30,
    )

    if connected_account.status != "ACTIVE":
        raise IntegrationAccessError(f"Composio connection not active: {connected_account.status}")

    # Try to extract provider email from auth params
    provider_email = _extract_composio_email(connected_account.id)

    integration, created = Integration.objects.update_or_create(
        tenant=tenant,
        provider=provider,
        defaults={
            "status": Integration.Status.ACTIVE,
            "composio_connected_account_id": connected_account.id,
            "provider_email": provider_email,
            "scopes": [],
            "key_vault_secret_name": "",
        },
    )

    logger.info(
        "%s Composio integration %s for tenant %s (account=%s)",
        "Created" if created else "Updated",
        provider,
        tenant.id,
        connected_account.id,
    )
    return integration


def _composio_val_to_dict(account) -> dict:
    """Safely extract a credentials dict from a Composio connected account.

    ``account.state.val`` may be a Pydantic model (composio-client >=1.27)
    or a plain dict; this helper normalises both to a dict.
    """
    val = getattr(getattr(account, "state", None), "val", None)
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if hasattr(val, "model_dump"):
        return val.model_dump()
    return {}


def _extract_composio_email(connected_account_id: str) -> str:
    """Best-effort email extraction from a Composio connected account."""
    try:
        client = _get_composio_client()
        account = client.connected_accounts.get(connected_account_id)
        credentials = _composio_val_to_dict(account)
        return credentials.get("email", "")
    except Exception:
        logger.warning(
            "Could not extract email from Composio account %s",
            connected_account_id,
            exc_info=True,
        )
    return ""


def _get_composio_access_token(
    integration: Integration,
    tenant: Tenant,
    provider: str,
) -> ProviderAccessToken:
    """Retrieve access token from Composio connected account."""
    if not integration.composio_connected_account_id:
        raise IntegrationTokenDataError(f"No Composio connected_account_id for provider={provider}")

    client = _get_composio_client()

    try:
        account = client.connected_accounts.get(
            integration.composio_connected_account_id,
        )
    except Exception as exc:
        logger.exception(
            "Composio connected_accounts.get failed for provider=%s connected_account_id=%s tenant=%s",
            provider,
            integration.composio_connected_account_id,
            tenant.id,
        )
        raise IntegrationRefreshError(f"Failed to retrieve Composio auth params for provider={provider}") from exc

    # Extract access_token from the connected account state
    credentials = _composio_val_to_dict(account)
    access_token = credentials.get("access_token", "")

    logger.debug(
        "Composio credentials for provider=%s: token_len=%d keys=%s",
        provider,
        len(access_token),
        sorted(credentials.keys()),
    )
    # Fall back: some providers put the token in an "Authorization" header value
    if not access_token:
        auth_header = credentials.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            access_token = auth_header[7:]
        else:
            access_token = auth_header

    if not access_token or access_token.endswith("..."):
        _mark_integration_status(integration, Integration.Status.ERROR)
        raise IntegrationTokenDataError(
            f"Composio returned masked/empty token for provider={provider}. "
            "Ensure 'Mask Connected Account Secrets' is disabled in Composio settings."
        )

    # Auto-recover: if we successfully got a token, clear any error status.
    if integration.status != Integration.Status.ACTIVE:
        logger.info(
            "Composio integration recovered for provider=%s tenant=%s (was status=%s)",
            provider,
            tenant.id,
            integration.status,
        )
        _mark_integration_status(integration, Integration.Status.ACTIVE)

    return ProviderAccessToken(
        access_token=access_token,
        expires_at=None,  # Composio manages expiry/refresh
        provider=provider,
        tenant_id=str(tenant.id),
    )


class IntegrationAccessError(RuntimeError):
    """Base error for brokered integration token access."""


class IntegrationNotConnectedError(IntegrationAccessError):
    """Integration record does not exist for tenant/provider."""


class IntegrationInactiveError(IntegrationAccessError):
    """Integration exists but is not usable (revoked/expired/error)."""


class IntegrationTokenDataError(IntegrationAccessError):
    """Token material is missing or malformed."""


class IntegrationProviderConfigError(IntegrationAccessError):
    """OAuth provider credentials are not configured in Django settings."""


class IntegrationRefreshError(IntegrationAccessError):
    """Refresh flow failed while trying to obtain a valid access token."""


class IntegrationScopeError(IntegrationAccessError):
    """Integration is connected but lacks required OAuth scopes."""


@dataclass(frozen=True)
class ProviderAccessToken:
    """Short-lived access token result exposed to internal callers."""

    access_token: str
    expires_at: datetime | None
    provider: str
    tenant_id: str


# OAuth provider configs
OAUTH_PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": [
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/tasks",
        ],
        "provider_group": "google",
    },
    "sautai": {
        "auth_url": "https://app.sautai.com/oauth/authorize",
        "token_url": "https://app.sautai.com/oauth/token",
        "scopes": ["read", "write"],
        "provider_group": "sautai",
    },
}

PROVIDER_GROUP = {
    Integration.Provider.GOOGLE: "google",
    Integration.Provider.SAUTAI: "sautai",
}

CLIENT_CREDENTIALS_BY_GROUP = {
    "google": (
        lambda: settings.GOOGLE_OAUTH_CLIENT_ID,
        lambda: settings.GOOGLE_OAUTH_CLIENT_SECRET,
    ),
    "sautai": (
        lambda: settings.SAUTAI_OAUTH_CLIENT_ID,
        lambda: settings.SAUTAI_OAUTH_CLIENT_SECRET,
    ),
}

READ_COMPATIBLE_SCOPES_BY_PROVIDER: dict[str, set[str]] = {
    Integration.Provider.GOOGLE: {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/tasks",
    },
}


def get_provider_config(provider: str) -> dict[str, Any]:
    """Return provider config or raise for unknown providers."""
    config = OAUTH_PROVIDERS.get(provider)
    if config is None:
        raise ValueError(f"Unsupported provider: {provider}")
    return config


def get_provider_client_credentials(provider: str) -> tuple[str, str]:
    """Return OAuth client credentials for a provider group."""
    group = PROVIDER_GROUP.get(provider, "")
    getters = CLIENT_CREDENTIALS_BY_GROUP.get(group)
    if getters is None:
        return "", ""
    return getters[0](), getters[1]()


def get_key_vault_secret_name(tenant: Tenant, provider: str) -> str:
    """Get the Key Vault secret name for a tenant's integration."""
    if not tenant.key_vault_prefix:
        raise ValueError(
            f"Tenant {tenant.id} has no key_vault_prefix — cannot build Key Vault secret name for {provider}"
        )
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
        _MOCK_KEY_VAULT_STORE[secret_name] = secret_value
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
        _MOCK_KEY_VAULT_STORE.pop(secret_name, None)
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


def load_tokens_from_key_vault(tenant: Tenant, provider: str) -> dict[str, Any] | None:
    """Load OAuth tokens from Azure Key Vault for a tenant/provider."""
    secret_name = get_key_vault_secret_name(tenant, provider)

    if os.environ.get("AZURE_MOCK", "false").lower() == "true":
        secret_value = _MOCK_KEY_VAULT_STORE.get(secret_name)
        if not secret_value:
            return None
        try:
            payload = json.loads(secret_value)
        except json.JSONDecodeError:
            logger.warning("[MOCK] Invalid JSON in Key Vault secret: %s", secret_name)
            return None
        if not isinstance(payload, dict):
            logger.warning("[MOCK] Token payload is not an object: %s", secret_name)
            return None
        return payload

    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    credential = DefaultAzureCredential()
    vault_url = f"https://{settings.AZURE_KEY_VAULT_NAME}.vault.azure.net"
    client = SecretClient(vault_url=vault_url, credential=credential)

    try:
        secret = client.get_secret(secret_name)
    except Exception:
        logger.warning("Failed to load Key Vault secret %s", secret_name)
        return None

    try:
        payload = json.loads(secret.value)
    except (TypeError, json.JSONDecodeError):
        logger.warning("Invalid JSON in Key Vault secret %s", secret_name)
        return None
    if not isinstance(payload, dict):
        logger.warning("Token payload is not an object in Key Vault secret %s", secret_name)
        return None
    return payload


def _mark_integration_status(integration: Integration, status: str) -> None:
    if integration.status != status:
        integration.status = status
        integration.save(update_fields=["status", "updated_at"])


def _is_access_token_expiring(
    token_expires_at: datetime | None,
    refresh_leeway_seconds: int,
) -> bool:
    if token_expires_at is None:
        return True
    threshold = timezone.now() + timedelta(seconds=refresh_leeway_seconds)
    return token_expires_at <= threshold


def _get_integration_or_raise(tenant: Tenant, provider: str) -> Integration:
    integration = Integration.objects.filter(tenant=tenant, provider=provider).first()
    if integration is None:
        raise IntegrationNotConnectedError(f"No integration configured for provider={provider}")

    if integration.status != Integration.Status.ACTIVE:
        raise IntegrationInactiveError(f"Integration status is {integration.status} for provider={provider}")

    return integration


def _has_read_compatible_scope(integration: Integration) -> bool:
    acceptable_scopes = READ_COMPATIBLE_SCOPES_BY_PROVIDER.get(integration.provider)
    if not acceptable_scopes:
        return True

    raw_scopes = integration.scopes
    if not isinstance(raw_scopes, list) or not raw_scopes:
        # Older rows may have empty scope metadata. Allow and rely on provider response.
        return True

    granted_scopes = {str(scope).strip() for scope in raw_scopes if isinstance(scope, str) and str(scope).strip()}
    return bool(granted_scopes.intersection(acceptable_scopes))


def get_valid_provider_access_token(
    tenant: Tenant,
    provider: str,
    refresh_leeway_seconds: int = ON_DEMAND_REFRESH_LEEWAY_SECONDS,
) -> ProviderAccessToken:
    """Return a valid access token for internal runtime calls.

    This broker never returns refresh tokens and may refresh on-demand
    when access tokens are missing or near expiry.
    """
    # Composio path: allow retry even when status is ERROR, since Composio
    # manages token refresh internally and transient failures should not
    # permanently block the integration.
    if is_composio_provider(provider):
        integration = Integration.objects.filter(tenant=tenant, provider=provider).first()
        if integration is None:
            raise IntegrationNotConnectedError(f"No integration configured for provider={provider}")
        if integration.composio_connected_account_id:
            return _get_composio_access_token(integration, tenant, provider)
        # Fall through to Key Vault path for legacy integrations without
        # a Composio connected account.

    # Non-Composio / legacy path: strict status check
    integration = _get_integration_or_raise(tenant, provider)

    # Existing Key Vault path (Sautai + legacy Google integrations)
    if not _has_read_compatible_scope(integration):
        raise IntegrationScopeError(f"Integration lacks read scope for provider={provider}; reconnect required")

    raw_tokens = load_tokens_from_key_vault(tenant, provider)
    tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    needs_refresh = not access_token or _is_access_token_expiring(integration.token_expires_at, refresh_leeway_seconds)

    if needs_refresh:
        if not refresh_token:
            _mark_integration_status(integration, Integration.Status.EXPIRED)
            raise IntegrationTokenDataError(f"Missing refresh_token for provider={provider}")

        client_id, client_secret = get_provider_client_credentials(provider)
        if not client_id or not client_secret:
            _mark_integration_status(integration, Integration.Status.ERROR)
            raise IntegrationProviderConfigError(f"Missing OAuth credentials for provider={provider}")

        try:
            integration = refresh_integration_tokens(
                tenant=tenant,
                provider=provider,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            new_status = Integration.Status.EXPIRED if status_code in (400, 401) else Integration.Status.ERROR
            _mark_integration_status(integration, new_status)
            raise IntegrationRefreshError(f"OAuth refresh failed for provider={provider} status={status_code}") from exc
        except Exception as exc:
            _mark_integration_status(integration, Integration.Status.ERROR)
            raise IntegrationRefreshError(f"OAuth refresh failed for provider={provider}") from exc

        raw_tokens = load_tokens_from_key_vault(tenant, provider)
        tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
        access_token = tokens.get("access_token")

    if not access_token:
        _mark_integration_status(integration, Integration.Status.ERROR)
        raise IntegrationTokenDataError(f"Missing access_token for provider={provider}")

    return ProviderAccessToken(
        access_token=access_token,
        expires_at=integration.token_expires_at,
        provider=provider,
        tenant_id=str(tenant.id),
    )


_REDDIT_ACTION_MAP: dict[str, str] = {
    "digest": "REDDIT_GET_R_TOP",  # top posts (can pass subreddit)
    "search": "REDDIT_SEARCH_ACROSS_SUBREDDITS",
    "my_activity": "REDDIT_GET_REDDIT_USER_ABOUT",  # user profile/activity
    "comments": "REDDIT_RETRIEVE_POST_COMMENTS",
    "post": "REDDIT_CREATE_REDDIT_POST",
    "reply": "REDDIT_POST_REDDIT_COMMENT",
    "edit": "REDDIT_EDIT_REDDIT_COMMENT_OR_POST",
    "delete_post": "REDDIT_DELETE_REDDIT_POST",
    "delete_comment": "REDDIT_DELETE_REDDIT_COMMENT",
    "new": "REDDIT_GET_NEW",  # new posts in subreddit
    "controversial": "REDDIT_GET_CONTROVERSIAL",
}


def execute_reddit_tool(tenant: Tenant, action: str, params: dict) -> dict:
    """Execute a Reddit action via Composio tools.

    Returns the result dict from Composio.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    tool_slug = _REDDIT_ACTION_MAP.get(action)
    if not tool_slug:
        raise ValueError(f"Unknown Reddit action: {action!r}. Valid actions: {sorted(_REDDIT_ACTION_MAP)}")

    from apps.integrations.models import Integration as _Integration

    integration = _Integration.objects.filter(
        tenant=tenant,
        provider="reddit",
        status=_Integration.Status.ACTIVE,
    ).first()
    if not integration or not integration.composio_connected_account_id:
        raise ValueError("Reddit integration not found or not fully connected for this tenant.")

    client = _get_composio_client()
    connected_account_id = integration.composio_connected_account_id

    _log.info("Executing Reddit tool %s for tenant %s", tool_slug, tenant.id)
    user_id = f"tenant-{tenant.id}"
    try:
        result = client.tools.execute(
            tool_slug,
            params,
            connected_account_id=connected_account_id,
            user_id=user_id,
            dangerously_skip_version_check=True,
        )
    except Exception as exc:
        err_str = str(exc)
        if "400" in err_str or "404" in err_str or "not found" in err_str.lower():
            _log.warning("Reddit connected account invalid for tenant %s: %s", tenant.id, exc)
            raise ValueError(
                "Reddit connection is no longer valid. Please reconnect in Settings → Integrations."
            ) from exc
        raise
    if not result.get("successful"):
        error_msg = result.get("error") or "Reddit tool returned an error"
        _log.error("Reddit tool %s failed for tenant %s: %s", tool_slug, tenant.id, error_msg)
        # Make missing-param errors readable for the agent
        if "fields are missing" in error_msg or "required" in error_msg.lower():
            raise RuntimeError(f"Missing required parameter for this Reddit action: {error_msg}")
        raise RuntimeError(error_msg)
    return result.get("data", result)


def _write_gws_credentials_to_file_share(
    tenant: Tenant,
    tokens: dict[str, Any],
) -> None:
    """Write gws CLI-compatible credentials JSON to the tenant's file share.

    The gws CLI reads this via GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE env var.
    Format: authorized_user credentials with client_id, client_secret, refresh_token.
    """
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        logger.warning("No refresh_token in tokens — cannot write gws credentials")
        return

    # Build gws-compatible authorized_user credentials
    client_id = str(getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", ""))
    client_secret = str(getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", ""))

    gws_creds = {
        "type": "authorized_user",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    share_name = f"ws-{str(tenant.id)[:20]}"
    creds_json = json.dumps(gws_creds)

    if os.environ.get("AZURE_MOCK", "false").lower() == "true":
        logger.info("[MOCK] Wrote gws credentials to file share %s", share_name)
        return

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    from azure.storage.fileshare import ShareFileClient

    from apps.orchestrator.azure_client import get_storage_client

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account_name,
    )
    account_key = keys.keys[0].value

    file_client = ShareFileClient(
        account_url=f"https://{account_name}.file.core.windows.net",
        share_name=share_name,
        file_path="gws-credentials.json",
        credential=account_key,
    )

    file_client.upload_file(creds_json.encode(), overwrite=True)
    logger.info("Wrote gws credentials to file share %s/gws-credentials.json", share_name)


def _delete_gws_credentials_from_file_share(tenant: Tenant) -> None:
    """Delete gws credentials from tenant's file share on disconnect."""
    share_name = f"ws-{str(tenant.id)[:20]}"

    if os.environ.get("AZURE_MOCK", "false").lower() == "true":
        logger.info("[MOCK] Deleted gws credentials from file share %s", share_name)
        return

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        return

    try:
        from azure.storage.fileshare import ShareFileClient

        from apps.orchestrator.azure_client import get_storage_client

        storage_client = get_storage_client()
        keys = storage_client.storage_accounts.list_keys(
            settings.AZURE_RESOURCE_GROUP,
            account_name,
        )
        account_key = keys.keys[0].value

        file_client = ShareFileClient(
            account_url=f"https://{account_name}.file.core.windows.net",
            share_name=share_name,
            file_path="gws-credentials.json",
            credential=account_key,
        )
        file_client.delete_file()
        logger.info("Deleted gws credentials from file share %s", share_name)
    except Exception:
        logger.debug("gws credentials file not found or delete failed for %s", share_name)


def connect_integration(
    tenant: Tenant,
    provider: str,
    tokens: dict[str, Any],
    provider_email: str | None = None,
    scopes: list[str] | None = None,
) -> Integration:
    """Connect an integration — store tokens and create/update record."""
    provider_config = get_provider_config(provider)
    secret_name = store_tokens_in_key_vault(tenant, provider, tokens)
    expires_in = tokens.get("expires_in")
    token_expires_at = None
    if expires_in is not None:
        token_expires_at = timezone.now() + timedelta(seconds=int(expires_in))

    defaults: dict[str, Any] = {
        "status": Integration.Status.ACTIVE,
        "scopes": scopes or provider_config.get("scopes", []),
        "key_vault_secret_name": secret_name,
        "token_expires_at": token_expires_at,
    }
    if provider_email is not None:
        defaults["provider_email"] = provider_email

    integration, created = Integration.objects.update_or_create(
        tenant=tenant,
        provider=provider,
        defaults=defaults,
    )

    logger.info(
        "%s integration %s for tenant %s",
        "Created" if created else "Updated",
        provider,
        tenant.id,
    )

    # For Google providers: write gws-compatible credentials to file share
    if provider_config.get("provider_group") == "google":
        try:
            _write_gws_credentials_to_file_share(tenant, tokens)
        except Exception:
            logger.warning(
                "Failed to write gws credentials to file share for tenant %s",
                tenant.id,
                exc_info=True,
            )

    return integration


def disconnect_integration(tenant: Tenant, provider: str) -> None:
    """Disconnect an integration — delete tokens and remove the record entirely."""
    integration = Integration.objects.filter(tenant=tenant, provider=provider).first()
    if not integration:
        return

    if integration.composio_connected_account_id:
        # Composio-managed: revoke the connected account on Composio side
        try:
            client = _get_composio_client()
            client.connected_accounts.delete(integration.composio_connected_account_id)
        except Exception:
            logger.warning(
                "Failed to delete Composio account %s for tenant %s — proceeding with local cleanup",
                integration.composio_connected_account_id,
                tenant.id,
            )
    else:
        # Key Vault path — best-effort token deletion
        try:
            delete_tokens_from_key_vault(tenant, provider)
        except Exception:
            logger.warning(
                "Failed to delete Key Vault secret for provider=%s tenant=%s — proceeding",
                provider,
                tenant.id,
            )

    # Clean up gws credentials from file share for Google providers
    provider_config = OAUTH_PROVIDERS.get(provider, {})
    if provider_config.get("provider_group") == "google":
        try:
            _delete_gws_credentials_from_file_share(tenant)
        except Exception:
            logger.debug("gws credentials cleanup failed for tenant %s", tenant.id)

    # Hard-delete the record so the UI shows a clean "Connect" state
    # (marking revoked leaves zombie records that confuse the UI and execute flow)
    integration.delete()
    logger.info("Disconnected and deleted integration %s for tenant %s", provider, tenant.id)


def refresh_integration_tokens(
    tenant: Tenant,
    provider: str,
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> Integration:
    """Refresh OAuth tokens and persist them to Key Vault + integration metadata."""
    if not refresh_token:
        raise ValueError("refresh_token is required")

    config = get_provider_config(provider)
    resp = httpx.post(
        config["token_url"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    payload.setdefault("refresh_token", refresh_token)

    scope_text = payload.get("scope", "")
    scopes = [s for s in scope_text.split(" ") if s] if scope_text else config.get("scopes", [])
    return connect_integration(
        tenant=tenant,
        provider=provider,
        tokens=payload,
        scopes=scopes,
    )
