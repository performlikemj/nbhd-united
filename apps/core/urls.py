from django.urls import path

from .runtime_views import (
    RuntimeCoreProfileView,
    RuntimeCoreSummaryView,
    RuntimeMeditationCreateView,
    RuntimeMeditationDetailView,
)
from .views import (
    CoreProfileView,
    CoreRestartView,
    CoreSettingsView,
    MeditationSessionDetailView,
    MeditationSessionListView,
)

urlpatterns = [
    # Consumer-facing (frontend, JWT auth)
    path("settings/", CoreSettingsView.as_view(), name="core-settings"),
    path("restart/", CoreRestartView.as_view(), name="core-restart"),
    path("profile/", CoreProfileView.as_view(), name="core-profile"),
    path("sessions/", MeditationSessionListView.as_view(), name="core-sessions"),
    path("sessions/<uuid:id>/", MeditationSessionDetailView.as_view(), name="core-session-detail"),
    # Internal runtime (OpenClaw plugin, X-NBHD-Internal-Key)
    path("runtime/<uuid:tenant_id>/summary/", RuntimeCoreSummaryView.as_view(), name="core-runtime-summary"),
    path("runtime/<uuid:tenant_id>/profile/", RuntimeCoreProfileView.as_view(), name="core-runtime-profile"),
    path(
        "runtime/<uuid:tenant_id>/meditation/",
        RuntimeMeditationCreateView.as_view(),
        name="core-runtime-meditation-create",
    ),
    path(
        "runtime/<uuid:tenant_id>/meditation/<uuid:meditation_id>/",
        RuntimeMeditationDetailView.as_view(),
        name="core-runtime-meditation-detail",
    ),
]
