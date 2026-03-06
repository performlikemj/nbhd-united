"""URLs for container → Django gate endpoints (scoped under tenant_id)."""
from django.urls import path

from .views import GateRequestView, GatePollView

urlpatterns = [
    path("request/", GateRequestView.as_view(), name="gate-request"),
    path("<int:action_id>/poll/", GatePollView.as_view(), name="gate-poll"),
]
