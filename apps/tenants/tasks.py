"""Celery tasks for tenant maintenance."""
from celery import shared_task
from django.utils import timezone

from .services import reset_daily_counters, reset_monthly_counters
from .telegram_models import TelegramLinkToken


@shared_task
def reset_daily_counters_task():
    """Reset daily message counters. Schedule: daily at midnight UTC."""
    return reset_daily_counters()


@shared_task
def reset_monthly_counters_task():
    """Reset monthly counters. Schedule: 1st of month at midnight UTC."""
    return reset_monthly_counters()


@shared_task
def cleanup_expired_telegram_tokens():
    """Purge expired and used TelegramLinkTokens older than 1 hour."""
    cutoff = timezone.now() - timezone.timedelta(hours=1)
    deleted, _ = TelegramLinkToken.objects.filter(
        expires_at__lt=cutoff
    ).delete()
    return f"Deleted {deleted} expired tokens"
