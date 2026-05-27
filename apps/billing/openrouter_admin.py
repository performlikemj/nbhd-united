"""OpenRouter management-API helpers for per-tenant sub-keys (PR #1.6).

This module owns all HTTP interaction with OpenRouter's key-management
surface. The platform's master account has one management key (stored in
central Key Vault) which can create / delete / inspect any sub-key under
the account. Each tenant gets a dedicated sub-key with a server-side
spending limit, so OR enforces the per-tenant cap and we don't have to
trust our internal estimate alone.

Two distinct credentials live here:

- **Management key** — admin scope; can create/delete keys; never
  injected into a per-tenant container. Read from KV via
  ``AZURE_KV_SECRET_OPENROUTER_MANAGEMENT_KEY``.
- **Per-tenant API key** — what a tenant's container uses for inference.
  The reconcile cron calls ``GET /api/v1/key`` *with that key as Bearer
  auth* to read its own ``usage_monthly`` (no management key needed for
  this path; the key authenticates the read about itself).

Endpoints used:

  ``POST /api/v1/keys``           — management key auth, create sub-key
  ``DELETE /api/v1/keys/{hash}``  — management key auth, delete sub-key
  ``GET /api/v1/key``             — per-tenant key auth, read own usage

Errors are raised as ``OpenRouterAdminError`` (a thin wrapper) so callers
can branch cleanly on the OR-side failure case vs unrelated exceptions.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class OpenRouterAdminError(Exception):
    """Raised when an OpenRouter management API call fails in a way the
    caller may want to handle distinctly (4xx, 5xx, network error).

    The wrapped HTTP response is available at ``self.response`` when one
    exists; ``self.status`` is the HTTP status code or ``None``.
    """

    def __init__(self, message: str, *, status: int | None = None, response: Any = None):
        super().__init__(message)
        self.status = status
        self.response = response


# Default per-request timeout for management calls. OR's management API
# is typically fast (sub-second); 15s gives generous headroom for
# transient slowness without letting a stuck request wedge a provisioning
# transaction.
_HTTP_TIMEOUT = 15.0


def _api_base() -> str:
    return str(getattr(settings, "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")).rstrip("/")


def _get_management_key() -> str:
    """Read the OR management key from central Key Vault.

    The management key is a credential of last resort — admin scope over
    the whole account — so we re-read it on every call rather than
    caching in process. Provision / deprovision flows fire on the order
    of seconds, not milliseconds; the extra KV round trip is invisible.

    Returns the secret string. Raises ``OpenRouterAdminError`` if the
    secret isn't configured or KV returns nothing (caller treats either
    case as "management ops unavailable").
    """
    # Lazy import so this module doesn't pull in azure_client during
    # Django startup (its KV credential helpers can be heavy).
    from apps.orchestrator.azure_client import read_key_vault_secret

    secret_name = str(getattr(settings, "AZURE_KV_SECRET_OPENROUTER_MANAGEMENT_KEY", "") or "").strip()
    if not secret_name:
        raise OpenRouterAdminError("AZURE_KV_SECRET_OPENROUTER_MANAGEMENT_KEY is not configured")

    value = read_key_vault_secret(secret_name)
    if not value:
        raise OpenRouterAdminError(f"OpenRouter management key not found at KV secret {secret_name!r}")
    return value.strip()


def _management_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_management_key()}",
        "Content-Type": "application/json",
    }


def _bearer_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def create_sub_key(label: str, limit_dollars: float, limit_reset: str = "monthly") -> tuple[str, str]:
    """Create a new OpenRouter sub-key with a server-side spending limit.

    Returns ``(api_key_string, key_hash)``. The api_key string is only
    visible once — the caller is responsible for persisting it (typically
    to per-tenant Key Vault) before returning to the orchestrator. The
    hash is OR's stable identifier; persist it on the Tenant row so we
    can target this key in future DELETE calls.

    ``limit_reset`` must be one of ``"daily" | "weekly" | "monthly"``.
    Raises ``OpenRouterAdminError`` on any non-2xx response, including
    a transport failure (so callers can branch on ``OpenRouterAdminError``
    without also catching httpx exceptions).
    """
    if limit_reset not in {"daily", "weekly", "monthly"}:
        raise OpenRouterAdminError(f"limit_reset must be daily/weekly/monthly, got {limit_reset!r}")

    body = {
        "name": label,
        "limit": float(limit_dollars),
        "limit_reset": limit_reset,
    }
    url = f"{_api_base()}/keys"
    try:
        resp = httpx.post(url, json=body, headers=_management_headers(), timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise OpenRouterAdminError(f"create_sub_key network failure: {exc}") from exc

    if resp.status_code >= 400:
        raise OpenRouterAdminError(
            f"create_sub_key HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code,
            response=resp,
        )

    payload = resp.json()
    api_key = str(payload.get("key") or "").strip()
    data = payload.get("data") or {}
    key_hash = str(data.get("hash") or "").strip()
    if not api_key or not key_hash:
        raise OpenRouterAdminError(
            f"create_sub_key response missing key/hash: keys={list(payload)} data_keys={list(data)}",
            status=resp.status_code,
            response=resp,
        )
    logger.info("openrouter_admin: created sub-key label=%s hash=%s", label, key_hash)
    return api_key, key_hash


def delete_sub_key(key_hash: str) -> None:
    """Delete an OpenRouter sub-key by hash. Idempotent: a 404 is treated
    as success (the key is already gone, which is what we wanted).

    Raises ``OpenRouterAdminError`` on any other non-2xx response.
    """
    key_hash = (key_hash or "").strip()
    if not key_hash:
        logger.info("openrouter_admin: delete_sub_key called with empty hash; no-op")
        return

    url = f"{_api_base()}/keys/{key_hash}"
    try:
        resp = httpx.delete(url, headers=_management_headers(), timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise OpenRouterAdminError(f"delete_sub_key network failure: {exc}") from exc

    if resp.status_code == 404:
        logger.info("openrouter_admin: delete_sub_key hash=%s already gone (404 treated as success)", key_hash)
        return
    if resp.status_code >= 400:
        raise OpenRouterAdminError(
            f"delete_sub_key HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code,
            response=resp,
        )
    logger.info("openrouter_admin: deleted sub-key hash=%s", key_hash)


def get_key_usage(api_key: str) -> Decimal:
    """Return ``usage_monthly`` for the given API key.

    Authenticated as the key itself (no management key required) — OR's
    ``GET /api/v1/key`` returns the metadata for whichever key signed
    the request. Used by the reconcile cron to true ``estimated_cost_this_month``
    up against provider truth, hourly.

    Returns ``Decimal(0)`` if the response is missing the field or the
    HTTP call fails — the reconcile cron logs + continues so one tenant's
    failure doesn't stop the others. Hard exceptions in caller code path
    (provisioning, etc.) should call the bare implementation if they
    want strict semantics.
    """
    api_key = (api_key or "").strip()
    if not api_key:
        return Decimal("0")

    url = f"{_api_base()}/key"
    try:
        resp = httpx.get(url, headers=_bearer_headers(api_key), timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("openrouter_admin: get_key_usage network failure: %s", exc)
        return Decimal("0")

    if resp.status_code >= 400:
        logger.warning("openrouter_admin: get_key_usage HTTP %s: %s", resp.status_code, resp.text[:200])
        return Decimal("0")

    payload = resp.json()
    data = payload.get("data") or payload
    usage_monthly = data.get("usage_monthly") if isinstance(data, dict) else None
    if usage_monthly is None:
        logger.warning(
            "openrouter_admin: get_key_usage response missing usage_monthly: keys=%s",
            list(data) if isinstance(data, dict) else type(data).__name__,
        )
        return Decimal("0")
    try:
        return Decimal(str(usage_monthly))
    except Exception:
        logger.warning("openrouter_admin: get_key_usage non-numeric usage_monthly=%r", usage_monthly)
        return Decimal("0")


def get_shared_key_usage() -> Decimal:
    """Convenience: ``get_key_usage`` against ``settings.OPENROUTER_API_KEY``.

    The shared key is what system-side OR calls (extraction, agenda
    hints, weekly synthesis) still use. The reconcile cron polls this
    alongside each tenant's sub-key to compute platform-wide MTD spend.
    """
    return get_key_usage(str(getattr(settings, "OPENROUTER_API_KEY", "") or ""))


def secret_name_for_tenant(tenant) -> str:
    """Build the per-tenant Key Vault secret name for the OR sub-key.

    Naming: ``<tenant.key_vault_prefix>-openrouter-key``. Mirrors the BYO
    naming convention so the existing per-tenant managed-identity KV
    access policies cover this secret without additional grants.
    """
    prefix = (getattr(tenant, "key_vault_prefix", "") or "").strip()
    if not prefix:
        raise OpenRouterAdminError(
            f"tenant {getattr(tenant, 'id', '?')} has no key_vault_prefix; cannot build OR sub-key secret name"
        )
    return f"{prefix}-openrouter-key"
