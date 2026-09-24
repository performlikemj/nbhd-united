"""Fail closed when an image-only operation would change runtime families."""

import re


def image_only_update_allowed(tenant, target_tag: str) -> bool:
    match = re.match(r"^(\d{4})\.(\d+)\.\d+(?:-|$)", target_tag or "")
    current = re.match(r"^(\d{4})\.(\d+)\.\d+$", tenant.openclaw_version or "")
    if not match or not current:
        return False  # Bare SHAs cannot establish the target binary version.
    record = getattr(tenant, "openclaw_migration", {}) or {}
    if record and record.get("status") != "PASS":
        return False
    return match.groups() == current.groups()


MIGRATION_REQUIRED = "Runtime family change or incomplete migration: use migrate_tenant_openclaw --tenant <uuid>."
