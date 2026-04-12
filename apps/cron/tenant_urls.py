from django.urls import path

from .tenant_views import (
    CronJobBulkDeleteView,
    CronJobBulkUpdateForegroundView,
    CronJobDetailView,
    CronJobListCreateView,
    CronJobToggleView,
)

urlpatterns = [
    path("", CronJobListCreateView.as_view(), name="cron-jobs-list-create"),
    path("bulk-delete/", CronJobBulkDeleteView.as_view(), name="cron-jobs-bulk-delete"),
    path("bulk-update-foreground/", CronJobBulkUpdateForegroundView.as_view(), name="cron-jobs-bulk-update-foreground"),
    path("<str:job_name>/", CronJobDetailView.as_view(), name="cron-jobs-detail"),
    path("<str:job_name>/toggle/", CronJobToggleView.as_view(), name="cron-jobs-toggle"),
]
