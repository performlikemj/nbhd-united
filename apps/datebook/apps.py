from django.apps import AppConfig


class DatebookConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.datebook"

    def ready(self) -> None:
        import apps.datebook.envelope  # noqa: F401
