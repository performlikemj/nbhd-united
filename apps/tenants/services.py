"""Tenant lifecycle services."""
from __future__ import annotations

import logging

from django.utils.text import slugify

from .models import Tenant, User

logger = logging.getLogger(__name__)


def create_tenant(
    display_name: str,
    telegram_chat_id: int,
    telegram_user_id: int | None = None,
    telegram_username: str = "",
    language: str = "en",
) -> Tenant:
    """Create a new tenant + user. Does NOT provision the container yet.

    Provisioning is triggered by billing webhook after payment.
    """
    # Generate unique username
    base_username = f"tg_{telegram_chat_id}"
    username = base_username

    user = User.objects.create_user(
        username=username,
        telegram_chat_id=telegram_chat_id,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username or "",
        display_name=display_name or "Friend",
        language=language,
    )

    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.PENDING,
        key_vault_prefix=f"tenants-{user.id}",
    )

    logger.info("Created tenant %s for user %s (chat_id=%s)", tenant.id, user.id, telegram_chat_id)
    return tenant


def reset_daily_counters() -> int:
    """Reset daily message counters. Run via Celery beat at midnight UTC."""
    count = Tenant.objects.filter(messages_today__gt=0).update(messages_today=0)
    logger.info("Reset daily counters for %d tenants", count)
    return count


def reset_monthly_counters() -> int:
    """Reset monthly counters. Run via Celery beat on 1st of month."""
    count = Tenant.objects.filter(messages_this_month__gt=0).update(
        messages_this_month=0,
        tokens_this_month=0,
        estimated_cost_this_month=0,
    )
    logger.info("Reset monthly counters for %d tenants", count)
    return count
