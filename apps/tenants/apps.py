from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenants"

    def ready(self):
        # Register the Profile section in the envelope registry.
        import apps.tenants.envelope  # noqa: F401

        # Wire the AgendaEngagement signal handlers (Phase B). The
        # pre_save snapshot can't be a @receiver — it needs to read the
        # prior row state, which means it must connect explicitly.
        from apps.tenants.agenda_signals import connect_signals

        connect_signals()
        # Importing the module also activates @receiver-decorated
        # post_save handlers.
        import apps.tenants.agenda_signals  # noqa: F401

        # Activate the pre_delete handler that hibernates a tenant's container
        # when its row is deleted (e.g. a User account cascade), so a teardown
        # blocked by the prod resource-group lock can't strand a running
        # container. See apps/tenants/signals.py + apps/orchestrator/orphan_reaper.py.
        import apps.tenants.signals  # noqa: F401
