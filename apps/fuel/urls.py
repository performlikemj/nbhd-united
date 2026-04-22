from django.urls import path

from .runtime_views import (
    RuntimeBodyWeightView,
    RuntimeFuelProfileView,
    RuntimeFuelSummaryView,
    RuntimeLogWorkoutView,
)
from .views import (
    BodyWeightDetailView,
    BodyWeightListView,
    FuelGoalDetailView,
    FuelGoalListView,
    FuelProfileView,
    FuelSettingsView,
    PRFeedView,
    RestingHRDetailView,
    RestingHRListView,
    WeeklyVolumeSummaryView,
    WorkoutCalendarView,
    WorkoutCountView,
    WorkoutDetailView,
    WorkoutDuplicateView,
    WorkoutListView,
    WorkoutProgressView,
    WorkoutTemplateDetailView,
    WorkoutTemplateListView,
)

urlpatterns = [
    # Consumer-facing (frontend, JWT auth)
    path("settings/", FuelSettingsView.as_view(), name="fuel-settings"),
    path("profile/", FuelProfileView.as_view(), name="fuel-profile"),
    path("workouts/", WorkoutListView.as_view(), name="fuel-workouts"),
    path("workouts/count/", WorkoutCountView.as_view(), name="fuel-workout-count"),
    path(
        "workouts/<uuid:workout_id>/",
        WorkoutDetailView.as_view(),
        name="fuel-workout-detail",
    ),
    path(
        "workouts/<uuid:workout_id>/duplicate/",
        WorkoutDuplicateView.as_view(),
        name="fuel-workout-duplicate",
    ),
    path("calendar/", WorkoutCalendarView.as_view(), name="fuel-calendar"),
    path("progress/", WorkoutProgressView.as_view(), name="fuel-progress"),
    path("weekly-summary/", WeeklyVolumeSummaryView.as_view(), name="fuel-weekly-summary"),
    path("templates/", WorkoutTemplateListView.as_view(), name="fuel-templates"),
    path(
        "templates/<uuid:template_id>/",
        WorkoutTemplateDetailView.as_view(),
        name="fuel-template-detail",
    ),
    path("prs/", PRFeedView.as_view(), name="fuel-prs"),
    path("goals/", FuelGoalListView.as_view(), name="fuel-goals"),
    path(
        "goals/<uuid:goal_id>/",
        FuelGoalDetailView.as_view(),
        name="fuel-goal-detail",
    ),
    path("body-weight/", BodyWeightListView.as_view(), name="fuel-body-weight"),
    path(
        "body-weight/<uuid:entry_id>/",
        BodyWeightDetailView.as_view(),
        name="fuel-body-weight-detail",
    ),
    path("resting-hr/", RestingHRListView.as_view(), name="fuel-resting-hr"),
    path(
        "resting-hr/<uuid:entry_id>/",
        RestingHRDetailView.as_view(),
        name="fuel-resting-hr-detail",
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
    path(
        "runtime/<uuid:tenant_id>/profile/",
        RuntimeFuelProfileView.as_view(),
        name="runtime-fuel-profile",
    ),
]
