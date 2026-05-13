from django.urls import path

from .runtime_views import (
    RuntimePillarCompareView,
    RuntimePillarHistoryView,
    RuntimePillarSnapshotDetailView,
)
from .views import PillarCompareView, PillarHistoryView, PillarSnapshotDetailView

urlpatterns = [
    # User-facing (JWT/PAT auth, tenant from request.user)
    path("history/", PillarHistoryView.as_view(), name="insights-history"),
    path(
        "snapshots/<uuid:snapshot_id>/",
        PillarSnapshotDetailView.as_view(),
        name="insights-snapshot-detail",
    ),
    path("compare/", PillarCompareView.as_view(), name="insights-compare"),
    # Internal runtime (X-NBHD-Internal-Key auth, tenant from URL).
    # Called by the nbhd-insights-tools OpenClaw plugin.
    path(
        "runtime/<uuid:tenant_id>/history/",
        RuntimePillarHistoryView.as_view(),
        name="runtime-insights-history",
    ),
    path(
        "runtime/<uuid:tenant_id>/snapshots/<uuid:snapshot_id>/",
        RuntimePillarSnapshotDetailView.as_view(),
        name="runtime-insights-snapshot-detail",
    ),
    path(
        "runtime/<uuid:tenant_id>/compare/",
        RuntimePillarCompareView.as_view(),
        name="runtime-insights-compare",
    ),
]
