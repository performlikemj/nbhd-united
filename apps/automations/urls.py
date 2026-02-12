from django.urls import path

from .views import (
    AutomationDetailView,
    AutomationListCreateView,
    AutomationManualRunView,
    AutomationPauseView,
    AutomationResumeView,
    AutomationRunsListView,
)

urlpatterns = [
    path("", AutomationListCreateView.as_view(), name="automations-list-create"),
    path("runs/", AutomationRunsListView.as_view(), name="automations-runs-list"),
    path("<uuid:automation_id>/", AutomationDetailView.as_view(), name="automations-detail"),
    path("<uuid:automation_id>/pause/", AutomationPauseView.as_view(), name="automations-pause"),
    path("<uuid:automation_id>/resume/", AutomationResumeView.as_view(), name="automations-resume"),
    path("<uuid:automation_id>/run/", AutomationManualRunView.as_view(), name="automations-run"),
    path("<uuid:automation_id>/runs/", AutomationRunsListView.as_view(), name="automations-runs-detail"),
]
