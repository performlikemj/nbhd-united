"""Client for invoking tools on a tenant's OpenClaw Gateway."""
from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

from apps.orchestrator.azure_client import read_key_vault_secret
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """Raised when a Gateway tool invocation fails."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _get_gateway_token(tenant: Tenant) -> str:
    """Read the tenant's internal API key from Key Vault."""
    secret_name = f"tenant-{tenant.id}-internal-key"
    token = read_key_vault_secret(secret_name)
    if not token:
        raise GatewayError(f"Could not read gateway token for tenant {tenant.id}")
    return token


def invoke_gateway_tool(tenant: Tenant, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a tool on a tenant's OpenClaw Gateway.

    Posts to ``https://{fqdn}/tools/invoke`` with the tool name and arguments.
    Returns the ``result`` field from the Gateway response.

    Raises ``GatewayError`` on failure.
    """
    if not tenant.container_fqdn:
        raise GatewayError(f"Tenant {tenant.id} has no container FQDN")

    token = _get_gateway_token(tenant)
    url = f"https://{tenant.container_fqdn}/tools/invoke"

    try:
        resp = requests.post(
            url,
            json={"tool": tool, "args": args},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise GatewayError(f"Gateway request failed: {exc}") from exc

    if resp.status_code != 200:
        raise GatewayError(
            f"Gateway returned {resp.status_code}: {resp.text[:500]}",
            status_code=resp.status_code,
        )

    data = resp.json()
    if not data.get("ok"):
        raise GatewayError(data.get("error", "Unknown gateway error"))

    return data.get("result", {})
