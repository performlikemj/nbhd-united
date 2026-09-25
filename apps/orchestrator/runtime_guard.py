"""Manual fleet tools only: refuse runtime-family crossings and ambiguous tags."""

import re


def _family(value):
    match = re.match(r"^(?:openclaw-)?(\d{4})\.(\d+)\.\d+(?:-|$)", value or "")
    return match.groups() if match else None


def image_only_update_allowed(tenant, target_tag: str) -> bool:
    record = getattr(tenant, "openclaw_migration", None) or {}
    if record.get("image_submitted") and record.get("status") != "PASS":
        return False
    target = _family(target_tag)
    # A DB version alone cannot identify a bare-SHA source image.
    source = _family(tenant.container_image_tag)
    current = _family(tenant.openclaw_version)
    if not (target and source == target and current == target):
        return False
    # Database tags cannot prove which runtime Azure is currently serving.
    try:
        from django.conf import settings

        from .azure_client import get_container_client

        app = get_container_client().container_apps.get(settings.AZURE_RESOURCE_GROUP, tenant.container_id)
        images = [c.image for c in app.template.containers if c.name == "openclaw"]
        if len(images) != 1 or not isinstance(images[0], str):
            return False
        live = re.fullmatch(r"[^/]+/nbhd-openclaw:([^/@]+)(?:@sha256:[0-9a-f]{64})?", images[0])
        return bool(live and _family(live[1]) == target)
    except Exception:
        # Metadata-only refusal; SDK errors can contain response bodies/secrets.
        return False


def manual_version_update_allowed(tenant, target_tag: str, target_version: str) -> bool:
    return image_only_update_allowed(tenant, target_tag) and _family(target_version) == _family(target_tag)


MIGRATION_REQUIRED = "Runtime family change or ambiguous image: use migrate_tenant_openclaw --tenant <uuid>."
