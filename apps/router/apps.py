from django.apps import AppConfig


class RouterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.router"

    def ready(self):
        # Wire the post_delete handler that busts the cached main-thread id when
        # an is_main ChatThread is deleted (so a delete+recreate can't serve a
        # dangling id from the ?since= feed cache). See apps/router/signals.py.
        import apps.router.signals  # noqa: F401
