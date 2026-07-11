from django.apps import AppConfig


class EvalsConfig(AppConfig):
    """Production eval system (Wave A). See docs/evals-directive.md.

    ``ready()`` intentionally stays EMPTY. Every app's ``ready()`` runs during
    ``manage.py migrate`` — before this app's own tables exist — so any eval
    bootstrapping there would crash every migration. Suites are invoked
    explicitly (QStash task / management command / CI), never at app-load.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.evals"
    verbose_name = "Evals"
