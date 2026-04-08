"""URL routing for tenant-facing workspace API."""
from django.urls import path

from .workspace_views import (
    WorkspaceDetailView,
    WorkspaceListCreateView,
    WorkspaceSwitchView,
)

urlpatterns = [
    path("", WorkspaceListCreateView.as_view(), name="workspace-list-create"),
    # NOTE: switch/ MUST come before <slug>/ to avoid the slug pattern catching it
    path("switch/", WorkspaceSwitchView.as_view(), name="workspace-switch"),
    path("<slug:slug>/", WorkspaceDetailView.as_view(), name="workspace-detail"),
]
