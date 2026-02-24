from django.urls import path

from . import views

urlpatterns = [
    path("trigger/<str:task_name>/", views.trigger_task, name="cron-trigger"),
    path("trigger-debug/<str:task_name>/", views.trigger_task_debug, name="cron-trigger-debug"),
    path("tasks/", views.list_tasks, name="cron-list-tasks"),
    path("apply-pending-configs/", views.apply_pending_configs, name="cron-apply-pending-configs"),
    path("force-reseed-crons/", views.force_reseed_crons, name="cron-force-reseed-crons"),
    path("restart-tenant-container/", views.restart_tenant_container, name="cron-restart-tenant-container"),
    path("expire-trials/", views.expire_trials, name="cron-expire-trials"),
]
