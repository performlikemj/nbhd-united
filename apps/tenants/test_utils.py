"""Shared test helpers for tenant fixtures."""

from __future__ import annotations

from django.conf import settings

from apps.tenants.models import Tenant


def seed_internal_key(tenant: Tenant, key: str | None = None) -> Tenant:
    """Give a test tenant a per-tenant internal API key.

    Phase 1d (2026-06-22) removed the legacy global `NBHD_INTERNAL_API_KEY`
    fallback from `validate_internal_runtime_request`, so a tenant must now
    carry its own `Tenant.internal_api_key` to authenticate internal runtime
    requests. Production tenants get this at provision time; test fixtures
    that build a tenant via `create_tenant`/`Tenant.objects.create` (which do
    not provision) need it stamped on explicitly.

    Defaults to `settings.NBHD_INTERNAL_API_KEY` — the value test classes set
    via `@override_settings(NBHD_INTERNAL_API_KEY="...")` and then send in the
    `X-NBHD-Internal-Key` header — so the stored per-tenant key matches what
    the test client presents. Pass `key` to override.

    Returns the tenant for chaining: `t = seed_internal_key(create_tenant(...))`.
    """
    value = key if key is not None else (getattr(settings, "NBHD_INTERNAL_API_KEY", "") or "")
    tenant.internal_api_key = value
    tenant.save(update_fields=["internal_api_key"])
    return tenant
