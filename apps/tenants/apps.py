from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenants"

    def ready(self):
        # Envelope registry sections. Registered here at boot so they're
        # active before any agent turn renders USER.md.
        # - tenants.envelope: Profile section
        # - orchestrator.agenda_envelope: Agenda meta-view across pillars
        #   (lives in orchestrator since it aggregates from many apps)
        import apps.orchestrator.agenda_envelope  # noqa: F401
        import apps.tenants.envelope  # noqa: F401
