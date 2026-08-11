from django.apps import AppConfig


class DatebookConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.datebook"

    def ready(self) -> None:
        # B2a owns envelope registration. B1 deliberately registers nothing.
        return None
