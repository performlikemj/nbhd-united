from django.urls import path

from .runtime_views import (
    RuntimeConfirmInsightView,
    RuntimeInsightListView,
    RuntimePillarBaselineView,
    RuntimePillarCompareView,
    RuntimePillarHistoryView,
    RuntimePillarSnapshotDetailView,
    RuntimeRecordInsightView,
    RuntimeRefuteInsightView,
)
from .views import (
    ConfirmInsightView,
    InsightListView,
    PillarBaselineView,
    PillarCompareView,
    PillarHistoryView,
    PillarSnapshotDetailView,
    RecordInsightView,
    RefuteInsightView,
)

urlpatterns = [
    # ── User-facing (JWT/PAT auth, tenant from request.user) ──────────────
    # Phase 1 — snapshot read tools
    path("history/", PillarHistoryView.as_view(), name="insights-history"),
    path(
        "snapshots/<uuid:snapshot_id>/",
        PillarSnapshotDetailView.as_view(),
        name="insights-snapshot-detail",
    ),
    path("compare/", PillarCompareView.as_view(), name="insights-compare"),
    # Phase 2 — baseline + memory of insights
    path("baseline/", PillarBaselineView.as_view(), name="insights-baseline"),
    path("insights/", InsightListView.as_view(), name="insights-list"),
    path("insights/record/", RecordInsightView.as_view(), name="insights-record"),
    path(
        "insights/<uuid:insight_id>/confirm/",
        ConfirmInsightView.as_view(),
        name="insights-confirm",
    ),
    path(
        "insights/<uuid:insight_id>/refute/",
        RefuteInsightView.as_view(),
        name="insights-refute",
    ),
    # ── Internal runtime (X-NBHD-Internal-Key auth, tenant from URL) ──────
    # Called by the nbhd-insights-tools OpenClaw plugin.
    # Phase 1
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
    # Phase 2
    path(
        "runtime/<uuid:tenant_id>/baseline/",
        RuntimePillarBaselineView.as_view(),
        name="runtime-insights-baseline",
    ),
    path(
        "runtime/<uuid:tenant_id>/insights/",
        RuntimeInsightListView.as_view(),
        name="runtime-insights-list",
    ),
    path(
        "runtime/<uuid:tenant_id>/insights/record/",
        RuntimeRecordInsightView.as_view(),
        name="runtime-insights-record",
    ),
    path(
        "runtime/<uuid:tenant_id>/insights/<uuid:insight_id>/confirm/",
        RuntimeConfirmInsightView.as_view(),
        name="runtime-insights-confirm",
    ),
    path(
        "runtime/<uuid:tenant_id>/insights/<uuid:insight_id>/refute/",
        RuntimeRefuteInsightView.as_view(),
        name="runtime-insights-refute",
    ),
]
