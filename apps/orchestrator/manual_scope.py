"""Explicit scopes for manual rollout boundaries only."""

from uuid import UUID


def tenant_scope(single=None, multiple=None):
    if (single is not None and multiple is not None) or bool(single) == bool(multiple):
        raise ValueError("Provide exactly one non-empty tenant scope")
    values = [single] if single else multiple
    if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError("Tenant scope must be a non-empty UUID list")
    try:
        return list(dict.fromkeys(str(UUID(v.strip())) for v in values))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Tenant scope must contain valid UUIDs") from None


def command_scope(options):
    multiple = options.get("tenants")
    if multiple is not None:
        multiple = multiple.split(",") if isinstance(multiple, str) else multiple
    return tenant_scope(options.get("tenant"), multiple)
