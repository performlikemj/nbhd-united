from django.urls import path

from .runtime_views import (
    RuntimeConfirmInsightView,
    RuntimeInsightListView,
    RuntimePillarBaselineView,
    RuntimePillarCompareView,
    RuntimePillarHistoryView,
    RuntimePillarSignalsView,
    RuntimePillarSnapshotDetailView,
    RuntimeRecordInsightView,
    RuntimeRefuteInsightView,
    RuntimeVoicePrefListView,
    RuntimeVoicePrefSetView,
    RuntimeYesterdaysSignalsView,
)
from .views import (
    ConfirmInsightView,
    InsightListView,
    PillarBaselineView,
    PillarCompareView,
    PillarHistoryView,
    PillarSignalsView,
    PillarSnapshotDetailView,
    RecordInsightView,
    RefuteInsightView,
    VoicePrefListView,
    VoicePrefSetView,
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
    # Phase 3 — graduated voice
    path("signals/", PillarSignalsView.as_view(), name="insights-signals"),
    path("voice-prefs/", VoicePrefListView.as_view(), name="insights-voice-prefs-list"),
    path("voice-prefs/set/", VoicePrefSetView.as_view(), name="insights-voice-prefs-set"),
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
    # Phase 3
    path(
        "runtime/<uuid:tenant_id>/signals/",
        RuntimePillarSignalsView.as_view(),
        name="runtime-insights-signals",
    ),
    path(
        "runtime/<uuid:tenant_id>/voice-prefs/",
        RuntimeVoicePrefListView.as_view(),
        name="runtime-insights-voice-prefs-list",
    ),
    path(
        "runtime/<uuid:tenant_id>/voice-prefs/set/",
        RuntimeVoicePrefSetView.as_view(),
        name="runtime-insights-voice-prefs-set",
    ),
    # Cross-pillar yesterday's-signals roll-up (PQ + HB prompts)
    path(
        "runtime/<uuid:tenant_id>/yesterdays-signals/",
        RuntimeYesterdaysSignalsView.as_view(),
        name="runtime-insights-yesterdays-signals",
    ),
]
