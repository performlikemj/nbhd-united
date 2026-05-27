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
