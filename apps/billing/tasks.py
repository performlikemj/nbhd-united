"""QStash task wrappers for billing cron jobs."""


def refresh_infra_costs_task():
    from apps.billing.infra_cost_service import refresh_infra_costs

    return refresh_infra_costs()


def reconcile_openrouter_spend_task():
    """Trues up internal usage counters against OpenRouter provider truth.

    See ``apps.billing.management.commands.reconcile_openrouter_spend`` for
    the implementation. Wrapped here so QStash can dispatch it via the
    ``trigger_task`` URL pattern in ``apps.cron.views``.
    """
    from apps.billing.management.commands.reconcile_openrouter_spend import reconcile_all

    return reconcile_all()


def model_health_check_task():
    """Probe the free-model offer (pricing + reachability) and flip it on a
    transition. See ``apps.billing.model_health.model_health_check``."""
    from apps.billing.model_health import model_health_check

    return model_health_check()
