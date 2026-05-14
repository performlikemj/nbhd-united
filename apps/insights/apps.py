from django.apps import AppConfig


class InsightsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.insights"

    def ready(self):
        # envelope.py registers the observation-mode section + auto-wires
        # USER.md refresh on AssistantInsight / PillarSnapshot changes.
        import apps.insights.envelope  # noqa: F401
