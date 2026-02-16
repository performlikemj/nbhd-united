from django.urls import path
from .views import PlatformIssueReportView

urlpatterns = [
    path(
        "<uuid:tenant_id>/report/",
        PlatformIssueReportView.as_view(),
        name="platform-issue-report",
    ),
]
