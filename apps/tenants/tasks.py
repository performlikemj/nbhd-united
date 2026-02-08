"""Celery tasks for tenant maintenance."""
from celery import shared_task

from .services import reset_daily_counters, reset_monthly_counters


@shared_task
def reset_daily_counters_task():
    """Reset daily message counters. Schedule: daily at midnight UTC."""
    return reset_daily_counters()


@shared_task
def reset_monthly_counters_task():
    """Reset monthly counters. Schedule: 1st of month at midnight UTC."""
    return reset_monthly_counters()
