from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenants"

    def ready(self):
        # Register the Profile section in the envelope registry.
        import apps.tenants.envelope  # noqa: F401
