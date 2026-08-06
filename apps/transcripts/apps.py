from django.apps import AppConfig


class TranscriptsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.transcripts"

    def ready(self):
        # Load the frozen AAD coordinate at app initialization, matching the
        # encryption-column declaration pattern used by sibling content apps.
        import apps.transcripts.enc_columns  # noqa: F401
