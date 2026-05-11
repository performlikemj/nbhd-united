from django.urls import path

from .pending_at_views import PendingAtCronCancelView, PendingAtCronView
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
    # Pending one-off (kind:"at") reminders live in a separate gateway-only
    # surface. Routes registered before the generic <job_name> path so the
    # literal "pending-at" prefix wins.
    path("pending-at/", PendingAtCronView.as_view(), name="cron-jobs-pending-at-list"),
    path("pending-at/<str:name>/", PendingAtCronCancelView.as_view(), name="cron-jobs-pending-at-cancel"),
    path("<str:job_name>/", CronJobDetailView.as_view(), name="cron-jobs-detail"),
    path("<str:job_name>/toggle/", CronJobToggleView.as_view(), name="cron-jobs-toggle"),
]
