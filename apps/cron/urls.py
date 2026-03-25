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
    path("bump-all-pending-configs/", views.bump_all_pending_configs, name="cron-bump-all-pending-configs"),
    path("resync-cron-timezones/", views.resync_cron_timezones, name="cron-resync-cron-timezones"),
    path("run-update-cron-prompts/", views.run_update_cron_prompts, name="cron-run-update-cron-prompts"),
    path("run-backfill-lesson-embeddings/", views.run_backfill_lesson_embeddings, name="cron-run-backfill-lesson-embeddings"),
    path("run-rewrite-lessons-actionable/", views.run_rewrite_lessons_actionable, name="cron-run-rewrite-lessons-actionable"),
    path("register-system-crons/", views.register_system_crons, name="cron-register-system-crons"),
    path("broadcast-message/", views.broadcast_message, name="cron-broadcast-message"),
    path("dedup-crons/", views.dedup_crons, name="cron-dedup-crons"),
]
