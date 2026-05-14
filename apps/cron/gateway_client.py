"""Client for invoking tools on a tenant's OpenClaw Gateway."""

from __future__ import annotations

import logging
from datetime import datetime
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


def get_gateway_token_for_tenant(tenant: Tenant) -> str:
    """Resolve the bearer token Django must send when calling this tenant's gateway.

    The container's gateway authenticates incoming requests against its
    `NBHD_INTERNAL_API_KEY` env var (resolved by Container Apps from a
    Key Vault secret reference). Django callers must send the SAME value
    or the gateway returns 401.

    Phase 1b/1c (2026-05-12) migrated tenants to per-tenant keys:
      1. If `Tenant.internal_api_key` is set, the container is using
         the per-tenant value — return it directly. DB is the source of
         truth post-migration; no KV round-trip on the hot path.
      2. Otherwise fall back to `settings.NBHD_INTERNAL_API_KEY` (the
         legacy shared value, still bound on unmigrated containers).

    Returns empty string when neither is available — caller decides how
    to handle (most paths bail out with a logged warning rather than
    sending a known-bad header).

    This is the public helper used by every Django→container code path
    (poller drain, hibernation flush, broadcast, gateway tool calls).
    See `_get_gateway_token` for the gateway-tool variant that raises
    `GatewayError` on miss.
    """
    per_tenant = (tenant.internal_api_key or "").strip()
    if per_tenant:
        return per_tenant
    return (getattr(settings, "NBHD_INTERNAL_API_KEY", "") or "").strip()


def _get_gateway_token(tenant: Tenant) -> str:
    """Variant of `get_gateway_token_for_tenant` that raises on miss.

    Used by `invoke_gateway_tool` where a missing token always indicates
    a real configuration failure (Django can't reach the gateway at all).
    KV is consulted as a last-resort fallback here because some Django
    startup paths historically loaded the gateway secret only from KV.
    """
    token = get_gateway_token_for_tenant(tenant)
    if not token:
        # Last-resort KV read — keeps the historical behaviour where a
        # Django pod without `settings.NBHD_INTERNAL_API_KEY` in its env
        # could still reach the gateway via KV.
        token = read_key_vault_secret("nbhd-internal-api-key") or ""
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

    # OpenClaw /tools/invoke expects {"tool": "<name>", "action": "<action>", "args": {}}
    # e.g. "cron.list" → tool="cron", action="list"
    if "." in tool:
        tool_name, action = tool.rsplit(".", 1)
    else:
        tool_name, action = tool, None

    body: dict[str, Any] = {"tool": tool_name, "args": args}
    if action:
        body["action"] = action

    # 45s timeout + one retry on timeout. The previous 15s would race with
    # cron fires at :00 of every hour — when the gateway is busy executing
    # an agent turn, ``cron.list`` could legitimately take 20-40s, and the
    # hourly reconcile sweep would time out and fail to detect drift. The
    # canary 2026-05-13 22:00 UTC incident hit this: Morning Briefing's
    # stale payload.model wasn't fixed before the 22:00 fire window
    # because the 22:00 reconcile timed out reading cron.list.
    last_exc: requests.RequestException | None = None
    for attempt in (1, 2):
        try:
            resp = requests.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=45,
            )
            break
        except requests.Timeout as exc:
            last_exc = exc
            if attempt == 1:
                logger.warning(
                    "Gateway %s.%s timed out (attempt 1/2) — retrying",
                    tool_name,
                    action or "",
                )
                continue
            raise GatewayError(f"Gateway request failed: {exc}") from exc
        except requests.RequestException as exc:
            raise GatewayError(f"Gateway request failed: {exc}") from exc
    else:  # pragma: no cover — defensive, the for-else only runs if no break
        raise GatewayError(f"Gateway request failed: {last_exc}")

    if resp.status_code != 200:
        logger.error(
            "Gateway %s.%s returned %s: %s",
            tool_name,
            action or "",
            resp.status_code,
            resp.text[:500],
        )
        raise GatewayError(
            f"Gateway returned {resp.status_code}: {resp.text[:500]}",
            status_code=resp.status_code,
        )

    data = resp.json()
    if not data.get("ok"):
        raise GatewayError(data.get("error", "Unknown gateway error"))

    return data.get("result", {})


def _next_fire_at(schedule: dict[str, Any]) -> datetime | None:
    """Compute the next scheduled fire time for a cron schedule dict.

    Accepts the gateway's schedule shape ``{"kind": "cron", "expr": ..., "tz": ...}``.
    Returns a timezone-aware datetime in the schedule's tz, or ``None`` if the
    expression cannot be parsed (caller should treat unknown as "fresh enough").
    """
    import zoneinfo

    from croniter import croniter

    expr = schedule.get("expr") if isinstance(schedule, dict) else None
    if not expr:
        return None
    tz_name = (schedule.get("tz") if isinstance(schedule, dict) else None) or "UTC"
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")
    try:
        return croniter(expr, datetime.now(tz)).get_next(datetime)
    except Exception:
        return None


def cron_exists(
    tenant: Tenant,
    cron_name: str,
    *,
    include_disabled: bool = True,
) -> bool:
    """Check whether a cron with the given name is currently scheduled.

    Returns True iff the gateway lists a job with the matching name.
    Callers that need staleness semantics (welcome-scheduler) should
    use ``cron_get`` and inspect the schedule directly.

    On any gateway error, returns False (conservative — caller will
    proceed with scheduling, which is preferable to silently swallowing
    a needed action).
    """
    return cron_get(tenant, cron_name, include_disabled=include_disabled) is not None


def cron_get(
    tenant: Tenant,
    cron_name: str,
    *,
    include_disabled: bool = True,
) -> dict[str, Any] | None:
    """Return the gateway's job dict for ``cron_name`` (or None).

    Used by welcome-scheduler to inspect the schedule of an existing
    cron and decide whether it's still pending or stale (a date-pattern
    one-shot whose date already passed without successful self-removal).
    """
    try:
        result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": include_disabled})
    except GatewayError:
        return None

    inner = result.get("details", result) if isinstance(result, dict) else result
    if isinstance(inner, dict):
        jobs = inner.get("jobs", [])
    elif isinstance(inner, list):
        jobs = inner
    else:
        jobs = []

    for job in jobs:
        if isinstance(job, dict) and job.get("name") == cron_name:
            return job
    return None


def cron_remove(tenant: Tenant, cron_name: str) -> None:
    """Remove a cron job by name from the tenant's gateway.

    Used by welcome schedulers to clear a stale one-shot before adding a
    fresh one. Idempotent at the gateway level — a missing job is not
    treated as an error.

    Raises ``GatewayError`` only on transport failure; missing-job
    responses are swallowed.
    """
    try:
        # Gateway's cron.remove expects ``jobId`` (which accepts either
        # the gateway's UUID or the cron's name field) — see existing
        # usages in apps/cron/tenant_views.py. Passing ``name`` returns
        # an HTTP 500 "tool execution failed" from the gateway.
        invoke_gateway_tool(tenant, "cron.remove", {"jobId": cron_name})
    except GatewayError as exc:
        # The gateway returns ok=false with "not found" when the cron is
        # already gone. Anything else is a real failure worth raising.
        msg = str(exc).lower()
        if "not found" in msg or "no such" in msg:
            return
        raise
