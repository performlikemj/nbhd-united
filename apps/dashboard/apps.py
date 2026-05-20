from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.dashboard"

    def ready(self):
        # Receivers bump the "dashboard" cache tag (see apps/common/cache.py)
        # on writes to models the dashboard reads. Without these, the
        # @tenant_cache(ttl=60, tag="dashboard") on DashboardView /
        # HorizonsView would lag user actions by up to a minute — and
        # the frontend's optimistic confirm/refute updates would get
        # silently reverted by a stale refetch from the cached response.
        import apps.dashboard.receivers  # noqa: F401
