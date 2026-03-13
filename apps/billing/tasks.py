"""QStash task wrappers for billing cron jobs."""


def refresh_infra_costs_task():
    from apps.billing.infra_cost_service import refresh_infra_costs

    return refresh_infra_costs()
