from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self):
        # envelope.py registers the Core USER.md section + auto-wires USER.md
        # refresh on MeditationSession changes. signals.py keeps the cache-tag
        # invalidation handler.
        import apps.core.envelope  # noqa: F401
        import apps.core.signals  # noqa: F401
