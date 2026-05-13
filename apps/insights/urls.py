from django.urls import path

from .views import PillarCompareView, PillarHistoryView, PillarSnapshotDetailView

urlpatterns = [
    path("history/", PillarHistoryView.as_view(), name="insights-history"),
    path("snapshots/<uuid:snapshot_id>/", PillarSnapshotDetailView.as_view(), name="insights-snapshot-detail"),
    path("compare/", PillarCompareView.as_view(), name="insights-compare"),
]
