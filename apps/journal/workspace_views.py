"""Tenant-facing REST API for workspace management.

JWT-authed endpoints used by the subscriber console (Next.js dashboard).
The runtime API at apps/integrations/runtime_views.py exposes the same
data via internal-key auth for the OpenClaw agent.

Both APIs share business logic from apps/journal/workspace_services.py.
"""
from __future__ import annotations

import logging

from django.http import Http404
from django.utils import timezone as tz
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.journal.models import Workspace
from apps.journal.workspace_services import (
    WORKSPACE_LIMIT,
    embed_workspace_description,
    ensure_default_workspace,
    generate_unique_slug,
    serialize_workspace,
)
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _get_tenant_for_user(user) -> Tenant:
    try:
        return user.tenant
    except Tenant.DoesNotExist as exc:
        raise Http404("Tenant not found.") from exc


class WorkspaceListCreateView(APIView):
    """List or create workspaces for the authenticated tenant.

    GET  /api/v1/workspaces/        — List workspaces
    POST /api/v1/workspaces/        — Create workspace {name, description}
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)

        workspaces = Workspace.objects.filter(tenant=tenant).order_by(
            "-is_default", "-last_used_at", "name"
        )
        active_id = tenant.active_workspace_id

        return Response(
            {
                "tenant_id": str(tenant.id),
                "workspaces": [
                    serialize_workspace(ws, active_workspace_id=active_id)
                    for ws in workspaces
                ],
                "active_workspace_id": str(active_id) if active_id else None,
                "limit": WORKSPACE_LIMIT,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)

        name = str(request.data.get("name", "")).strip()
        description = str(request.data.get("description", "")).strip()

        if not name:
            return Response(
                {"error": "invalid_request", "detail": "name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(name) > 60:
            return Response(
                {"error": "invalid_request", "detail": "name must be 60 characters or less"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Auto-create the default workspace on first creation
        is_first_create = not Workspace.objects.filter(tenant=tenant).exists()
        if is_first_create:
            ensure_default_workspace(tenant)

        # Enforce max workspaces per tenant
        if Workspace.objects.filter(tenant=tenant).count() >= WORKSPACE_LIMIT:
            return Response(
                {
                    "error": "workspace_limit_reached",
                    "detail": f"Maximum {WORKSPACE_LIMIT} workspaces per tenant",
                },
                status=status.HTTP_409_CONFLICT,
            )

        slug = generate_unique_slug(tenant, name)
        workspace = Workspace.objects.create(
            tenant=tenant,
            name=name,
            slug=slug,
            description=description,
            description_embedding=embed_workspace_description(description),
            is_default=False,
        )

        # Make the new workspace active
        tenant.active_workspace = workspace
        tenant.save(update_fields=["active_workspace"])

        return Response(
            {
                "tenant_id": str(tenant.id),
                "workspace": serialize_workspace(
                    workspace, active_workspace_id=workspace.id
                ),
                "default_workspace_created": is_first_create,
            },
            status=status.HTTP_201_CREATED,
        )


class WorkspaceDetailView(APIView):
    """Update or delete a single workspace by slug.

    PATCH  /api/v1/workspaces/<slug>/   — Update {name?, description?}
    DELETE /api/v1/workspaces/<slug>/   — Delete (not default)
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request, slug):
        tenant = _get_tenant_for_user(request.user)

        workspace = Workspace.objects.filter(tenant=tenant, slug=slug).first()
        if workspace is None:
            return Response(
                {"error": "workspace_not_found", "detail": f"No workspace with slug {slug!r}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        updated_fields: list[str] = []

        if "name" in request.data:
            new_name = str(request.data.get("name", "")).strip()
            if not new_name:
                return Response(
                    {"error": "invalid_request", "detail": "name cannot be empty"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(new_name) > 60:
                return Response(
                    {"error": "invalid_request", "detail": "name must be 60 characters or less"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            workspace.name = new_name
            updated_fields.append("name")

        if "description" in request.data:
            new_description = str(request.data.get("description", "")).strip()
            workspace.description = new_description
            workspace.description_embedding = embed_workspace_description(new_description)
            updated_fields.extend(["description", "description_embedding"])

        if updated_fields:
            workspace.save(update_fields=updated_fields)

        return Response(
            {
                "tenant_id": str(tenant.id),
                "workspace": serialize_workspace(
                    workspace, active_workspace_id=tenant.active_workspace_id
                ),
                "updated": updated_fields,
            },
            status=status.HTTP_200_OK,
        )

    def delete(self, request, slug):
        tenant = _get_tenant_for_user(request.user)

        workspace = Workspace.objects.filter(tenant=tenant, slug=slug).first()
        if workspace is None:
            return Response(
                {"error": "workspace_not_found", "detail": f"No workspace with slug {slug!r}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if workspace.is_default:
            return Response(
                {
                    "error": "cannot_delete_default",
                    "detail": "Cannot delete the default workspace",
                },
                status=status.HTTP_409_CONFLICT,
            )

        # If deleting the active workspace, fall back to default
        was_active = tenant.active_workspace_id == workspace.id
        if was_active:
            default_ws = Workspace.objects.filter(
                tenant=tenant, is_default=True
            ).first()
            tenant.active_workspace = default_ws
            tenant.save(update_fields=["active_workspace"])

        deleted_id = str(workspace.id)
        workspace.delete()

        return Response(
            {
                "tenant_id": str(tenant.id),
                "deleted_id": deleted_id,
                "fell_back_to_default": was_active,
            },
            status=status.HTTP_200_OK,
        )


class WorkspaceSwitchView(APIView):
    """Switch the active workspace for the authenticated tenant.

    POST /api/v1/workspaces/switch/  — Body: {slug}
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)

        slug = str(request.data.get("slug", "")).strip()
        if not slug:
            return Response(
                {"error": "invalid_request", "detail": "slug is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        workspace = Workspace.objects.filter(tenant=tenant, slug=slug).first()
        if workspace is None:
            return Response(
                {"error": "workspace_not_found", "detail": f"No workspace with slug {slug!r}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        previous_id = tenant.active_workspace_id
        tenant.active_workspace = workspace
        tenant.save(update_fields=["active_workspace"])

        workspace.last_used_at = tz.now()
        workspace.save(update_fields=["last_used_at"])

        return Response(
            {
                "tenant_id": str(tenant.id),
                "workspace": serialize_workspace(
                    workspace, active_workspace_id=workspace.id
                ),
                "previous_workspace_id": str(previous_id) if previous_id else None,
            },
            status=status.HTTP_200_OK,
        )
