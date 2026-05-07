from django.apps import AppConfig


class CronConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cron"

    def ready(self):
        import apps.cron.signals  # noqa: F401
