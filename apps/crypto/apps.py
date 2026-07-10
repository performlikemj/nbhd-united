from django.apps import AppConfig


class CryptoConfig(AppConfig):
    """Envelope-encryption key service (encryption-at-rest Phase 1).

    Model-less app (the one model, ``TenantDek``, lives on ``apps.tenants``
    so it can FK ``Tenant`` cleanly and ride the tenants migration graph).
    Registered here purely for app discovery.

    ``ready()`` intentionally stays EMPTY. DEK pre-warm (populating the
    per-process cache ahead of the first request) is Phase 1 PR4 and hooks
    into ``gunicorn.conf.py::post_worker_init`` / ``poller.py::start()`` —
    NEVER ``AppConfig.ready()``. Thirteen apps' ``ready()`` run during
    ``manage.py migrate``, before the ``tenant_deks`` table (or its columns)
    exist; a pre-warm sweep there would crash every migration.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.crypto"
