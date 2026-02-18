from django.urls import path

from . import views

urlpatterns = [
    path("trigger/<str:task_name>/", views.trigger_task, name="cron-trigger"),
    path("trigger-debug/<str:task_name>/", views.trigger_task_debug, name="cron-trigger-debug"),
    path("tasks/", views.list_tasks, name="cron-list-tasks"),
    path("apply-pending-configs/", views.apply_pending_configs, name="cron-apply-pending-configs"),
]
