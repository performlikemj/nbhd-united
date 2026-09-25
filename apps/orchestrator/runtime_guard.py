"""Manual fleet tools only: refuse runtime-family crossings and ambiguous tags."""

import re


def _family(value):
    match = re.match(r"^(?:openclaw-)?(\d{4})\.(\d+)\.\d+(?:-|$)", value or "")
    return match.groups() if match else None


def image_only_update_allowed(tenant, target_tag: str) -> bool:
    target = _family(target_tag)
    # A DB version alone cannot identify a bare-SHA source image.
    source = _family(tenant.container_image_tag)
    current = _family(tenant.openclaw_version)
    return bool(target and source == target and current == target)


def manual_version_update_allowed(tenant, target_tag: str, target_version: str) -> bool:
    return image_only_update_allowed(tenant, target_tag) and _family(target_version) == _family(target_tag)


MIGRATION_REQUIRED = "Runtime family change or ambiguous image: use migrate_tenant_openclaw --tenant <uuid>."
