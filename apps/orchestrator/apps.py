from django.apps import AppConfig


class OrchestratorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orchestrator"

    def ready(self):
        # Envelope registry — orchestrator-level sections that aggregate
        # across pillar apps. Pillar-specific sections register from
        # their own ready() (apps.tenants, apps.fuel, apps.finance, etc.).
        import apps.orchestrator.agenda_envelope  # noqa: F401
