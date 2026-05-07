from django.apps import AppConfig


class FuelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.fuel"

    def ready(self):
        # signals.py keeps the cron-regen handler for session-scheduling
        # (post_save on Workout → debounced regenerate_fuel_crons). The
        # envelope module registers the Fuel section + auto-wires USER.md
        # refresh on Workout/BodyWeightLog/SleepLog changes.
        import apps.fuel.envelope  # noqa: F401
        import apps.fuel.signals  # noqa: F401
