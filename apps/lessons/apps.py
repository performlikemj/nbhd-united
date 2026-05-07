from django.apps import AppConfig


class LessonsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.lessons"

    def ready(self):
        # Register the Recent lessons section in the envelope registry.
        # Signal handlers are auto-wired by ``register_section``.
        import apps.lessons.envelope  # noqa: F401
