"""Independent Living Chat / Talk rollout gates.

Each setting is a comma list of tenant UUIDs. Fail-closed: empty or unset means
nobody. A lone ``*`` token (exact, after trimming) means every tenant — anything
else that merely contains ``*`` is ignored. ``CHAT_PANELS_TOOL_TENANT_IDS=*`` is
only safe once EVERY running tenant image includes the #1638 plugin manifest.
"""

from uuid import UUID

from django.conf import settings


def _tenant_allowed(tenant, setting: str) -> bool:
    raw = str(getattr(settings, setting, "") or "")
    tokens = {part.strip() for part in raw.split(",") if part.strip()}
    try:
        tenant_id = UUID(str(tenant.id)) if tenant is not None else None
    except (AttributeError, ValueError):
        return False
    if tenant_id is None:
        return False
    if "*" in tokens:
        return True
    allowed = set()
    for token in tokens:
        try:
            allowed.add(UUID(token))
        except ValueError:
            continue
    return tenant_id in allowed


def chat_shape_enabled(tenant) -> bool:
    """Image-independent shape endpoint, chat instruction and fenced panels."""
    return _tenant_allowed(tenant, "CHAT_SHAPE_TENANT_IDS")


def chat_panels_tool_enabled(tenant) -> bool:
    """Tool panels: allowlist only after verifying the running plugin manifest."""
    return _tenant_allowed(tenant, "CHAT_PANELS_TOOL_TENANT_IDS")


def talk_route_enabled(tenant) -> bool:
    """Talk-mode ack/quick-read router (Django-only; image-independent)."""
    return _tenant_allowed(tenant, "TALK_ROUTE_TENANT_IDS")


def web_redesign_enabled(tenant) -> bool:
    """Open Sky web console (read by the frontend via /tenants/me/)."""
    return _tenant_allowed(tenant, "WEB_REDESIGN_TENANT_IDS")
