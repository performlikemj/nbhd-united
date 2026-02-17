from django.urls import path

from .tenant_views import CronJobDetailView, CronJobListCreateView, CronJobToggleView

urlpatterns = [
    path("", CronJobListCreateView.as_view(), name="cron-jobs-list-create"),
    path("<str:job_name>/", CronJobDetailView.as_view(), name="cron-jobs-detail"),
    path("<str:job_name>/toggle/", CronJobToggleView.as_view(), name="cron-jobs-toggle"),
]
