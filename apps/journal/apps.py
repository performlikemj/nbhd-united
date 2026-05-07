from django.apps import AppConfig


class JournalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.journal"

    def ready(self):
        # signals.py keeps the QStash memory-sync handler (registry doesn't
        # cover that). envelope.py registers Goals / Open tasks / Recent
        # journal sections + auto-wires Document signal handlers.
        import apps.journal.envelope  # noqa: F401
        import apps.journal.signals  # noqa: F401
