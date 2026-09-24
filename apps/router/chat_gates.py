"""Independent Living Chat rollout gates; explicit tenant IDs, never wildcards."""

from django.conf import settings


def _tenant_allowed(tenant, setting: str) -> bool:
    raw = str(getattr(settings, setting, "") or "")
    allowed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return tenant is not None and str(tenant.id).lower() in allowed


def chat_shape_enabled(tenant) -> bool:
    """Image-independent shape endpoint, chat instruction and fenced panels."""
    return _tenant_allowed(tenant, "CHAT_SHAPE_TENANT_IDS")


def chat_panels_tool_enabled(tenant) -> bool:
    """Tool panels: allowlist only after verifying the running plugin manifest."""
    return _tenant_allowed(tenant, "CHAT_PANELS_TOOL_TENANT_IDS")
