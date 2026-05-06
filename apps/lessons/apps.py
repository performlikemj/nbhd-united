from django.apps import AppConfig


class LessonsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.lessons"

    def ready(self):
        import apps.lessons.signals  # noqa: F401
