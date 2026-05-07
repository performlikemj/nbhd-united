from django.apps import AppConfig


class FinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.finance"

    def ready(self):
        # Register the Gravity finance section in the envelope registry.
        # Signal handlers for FinanceAccount/Transaction/PayoffPlan are
        # auto-wired by ``register_section`` — no separate signals.py.
        import apps.finance.envelope  # noqa: F401
