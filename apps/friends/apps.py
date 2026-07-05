from django.apps import AppConfig


class FriendsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.friends"

    def ready(self):
        # Register the Neighborhood envelope section in the envelope registry.
        # Signal handlers are auto-wired by ``register_section``.
        import apps.friends.envelope  # noqa: F401

        # Wire the journal.Task → Mission completion linkage receiver.
        from apps.friends import task_links

        task_links.connect()
