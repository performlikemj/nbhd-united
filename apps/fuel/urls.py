from django.urls import path

from .runtime_views import (
    RuntimeBodyWeightView,
    RuntimeFuelSummaryView,
    RuntimeLogWorkoutView,
)
from .views import (
    BodyWeightDetailView,
    BodyWeightListView,
    FuelSettingsView,
    WorkoutCalendarView,
    WorkoutDetailView,
    WorkoutListView,
    WorkoutProgressView,
)

urlpatterns = [
    # Consumer-facing (frontend, JWT auth)
    path("settings/", FuelSettingsView.as_view(), name="fuel-settings"),
    path("workouts/", WorkoutListView.as_view(), name="fuel-workouts"),
    path(
        "workouts/<uuid:workout_id>/",
        WorkoutDetailView.as_view(),
        name="fuel-workout-detail",
    ),
    path("calendar/", WorkoutCalendarView.as_view(), name="fuel-calendar"),
    path("progress/", WorkoutProgressView.as_view(), name="fuel-progress"),
    path("body-weight/", BodyWeightListView.as_view(), name="fuel-body-weight"),
    path(
        "body-weight/<uuid:entry_id>/",
        BodyWeightDetailView.as_view(),
        name="fuel-body-weight-detail",
    ),
    # Runtime (OpenClaw plugin, internal auth)
    path(
        "runtime/<uuid:tenant_id>/log/",
        RuntimeLogWorkoutView.as_view(),
        name="runtime-fuel-log",
    ),
    path(
        "runtime/<uuid:tenant_id>/summary/",
        RuntimeFuelSummaryView.as_view(),
        name="runtime-fuel-summary",
    ),
    path(
        "runtime/<uuid:tenant_id>/body-weight/",
        RuntimeBodyWeightView.as_view(),
        name="runtime-fuel-body-weight",
    ),
]
