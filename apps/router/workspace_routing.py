"""Workspace routing for OpenClaw session isolation.

Routes incoming messages to workspace-specific OpenClaw sessions by varying
the `user` param in /v1/chat/completions calls. Each distinct `user` value
creates an independent conversation context within the same agent.

Default workspace uses the bare base_user_id (no suffix), preserving the
existing session for users who had conversation history before workspaces
were enabled. Non-default workspaces append `:ws:{slug}`.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from apps.journal.models import Workspace
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

SESSION_GAP_SECONDS = 30 * 60  # Match poller.py — must stay in sync
CLASSIFICATION_THRESHOLD = 0.5  # Minimum cosine similarity to switch workspaces


def resolve_workspace_routing(
    tenant: "Tenant",
    base_user_id: str,
    message_text: str,
) -> tuple[str, "Workspace | None", bool]:
    """Decide which workspace this message belongs to and return the user_param.

    Returns:
        (user_param, workspace, transitioned)
        - user_param: string to pass as `user` in /v1/chat/completions
        - workspace: the resolved Workspace, or None if tenant has no workspaces
        - transitioned: True if the active workspace changed (for chip injection)
    """
    workspaces = list(tenant.workspaces.all())

    # No workspaces → legacy behavior, no routing
    if not workspaces:
        return base_user_id, None, False

    current_active = tenant.active_workspace

    # Within active session → stay in current workspace, no classification
    if not _is_new_session(tenant):
        target = current_active or _get_default(workspaces)
        return _build_user_param(base_user_id, target), target, False

    # New session — classify the message to find best workspace
    classified = _classify_message(message_text, workspaces)
    target = classified or current_active or _get_default(workspaces)

    transitioned = bool(current_active and target and current_active.id != target.id)
    return _build_user_param(base_user_id, target), target, transitioned


def update_active_workspace(tenant: "Tenant", workspace: "Workspace | None") -> None:
    """Persist the routing decision to the tenant and workspace."""
    if workspace is None:
        return

    now = timezone.now()
    fields_to_update = []

    if tenant.active_workspace_id != workspace.id:
        tenant.active_workspace = workspace
        fields_to_update.append("active_workspace")

    if fields_to_update:
        tenant.save(update_fields=fields_to_update)

    # Update last_used_at on the workspace itself (separate save to avoid stale tenant)
    workspace.last_used_at = now
    workspace.save(update_fields=["last_used_at"])


def build_transition_marker(workspace: "Workspace") -> str:
    """Build the prefix that tells the agent it just switched workspaces."""
    return f"[Switched to {workspace.name} workspace. Add the chip indicator on your first response.]\n\n"


# ── Internal helpers ─────────────────────────────────────────────────────


def _is_new_session(tenant: "Tenant") -> bool:
    """Match the poller's session-gap logic."""
    if not tenant.last_message_at:
        return True
    elapsed = (timezone.now() - tenant.last_message_at).total_seconds()
    return elapsed > SESSION_GAP_SECONDS


def _get_default(workspaces: list["Workspace"]) -> "Workspace | None":
    """Return the default workspace, falling back to the first one."""
    for ws in workspaces:
        if ws.is_default:
            return ws
    return workspaces[0] if workspaces else None


def _build_user_param(base_user_id: str, workspace: "Workspace | None") -> str:
    """Default workspace = bare base_user_id; non-default = with suffix."""
    if workspace is None or workspace.is_default:
        return base_user_id
    return f"{base_user_id}:ws:{workspace.slug}"


def _classify_message(
    message_text: str,
    workspaces: list["Workspace"],
) -> "Workspace | None":
    """Find the workspace whose description best matches the message.

    Returns None if no workspace is confident enough or if classification fails.
    """
    # Filter workspaces with embeddings
    candidates = [ws for ws in workspaces if ws.description_embedding is not None]
    if not candidates:
        return None

    try:
        from apps.lessons.services import generate_embedding
        from pgvector.django import CosineDistance
        from apps.journal.models import Workspace

        query_embedding = generate_embedding(message_text[:500])
        if query_embedding is None:
            return None

        # Score each candidate by cosine similarity
        best_workspace = None
        best_similarity = 0.0

        # Re-query to use pgvector's distance ordering on the DB side
        scored = (
            Workspace.objects
            .filter(id__in=[ws.id for ws in candidates])
            .annotate(distance=CosineDistance("description_embedding", query_embedding))
            .order_by("distance")
        )

        for ws in scored:
            similarity = 1.0 - float(ws.distance)
            if similarity > best_similarity and similarity >= CLASSIFICATION_THRESHOLD:
                best_similarity = similarity
                best_workspace = ws
                break  # ordered by distance, first match is best

        return best_workspace

    except Exception:
        logger.exception("workspace_routing: classification failed")
        return None
