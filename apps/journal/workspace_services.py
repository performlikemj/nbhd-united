"""Workspace business logic shared by runtime and user-facing endpoints.

These helpers were originally defined in apps/integrations/runtime_views.py for
Phase 3 (agent-facing runtime API). Phase 5 adds user-facing endpoints in
apps/journal/workspace_views.py that need the same logic, so they're extracted
here for clean reuse.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.journal.models import Workspace
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

WORKSPACE_LIMIT = 4
DEFAULT_WORKSPACE_NAME = "General"
DEFAULT_WORKSPACE_SLUG = "general"
DEFAULT_WORKSPACE_DESCRIPTION = (
    "Catch-all workspace for everyday conversations and topics that don't "
    "fit into a more specific workspace."
)


def serialize_workspace(workspace: "Workspace", *, active_workspace_id=None) -> dict:
    """Serialize a Workspace model to JSON for API responses."""
    return {
        "id": str(workspace.id),
        "name": workspace.name,
        "slug": workspace.slug,
        "description": workspace.description,
        "is_default": workspace.is_default,
        "is_active": (
            active_workspace_id is not None
            and str(workspace.id) == str(active_workspace_id)
        ),
        "created_at": workspace.created_at.isoformat() if workspace.created_at else None,
        "last_used_at": (
            workspace.last_used_at.isoformat() if workspace.last_used_at else None
        ),
    }


def generate_unique_slug(tenant: "Tenant", base_slug: str) -> str:
    """Generate a slug unique within the tenant.

    Uses Django's slugify and appends -2, -3, ... on collision.
    """
    from apps.journal.models import Workspace
    from django.utils.text import slugify

    base = slugify(base_slug) or "workspace"
    slug = base
    suffix = 2
    while Workspace.objects.filter(tenant=tenant, slug=slug).exists():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def embed_workspace_description(description: str):
    """Generate an embedding for the description, returning None on failure.

    Failures are logged and swallowed so workspace creation/update never fails
    just because the embedding service is down. Workspaces without embeddings
    are skipped during routing classification (still usable as fallback).
    """
    description = (description or "").strip()
    if not description:
        return None
    try:
        from apps.lessons.services import generate_embedding
        return generate_embedding(description)
    except Exception:
        logger.exception("workspace: failed to embed description")
        return None


def ensure_default_workspace(tenant: "Tenant") -> "Workspace":
    """Create the General default workspace if the tenant has none.

    Called automatically when creating a tenant's first workspace so they
    always have a fallback to route to. Returns the existing default if one
    already exists.
    """
    from apps.journal.models import Workspace

    existing_default = Workspace.objects.filter(tenant=tenant, is_default=True).first()
    if existing_default is not None:
        return existing_default

    return Workspace.objects.create(
        tenant=tenant,
        name=DEFAULT_WORKSPACE_NAME,
        slug=DEFAULT_WORKSPACE_SLUG,
        description=DEFAULT_WORKSPACE_DESCRIPTION,
        description_embedding=embed_workspace_description(DEFAULT_WORKSPACE_DESCRIPTION),
        is_default=True,
    )
