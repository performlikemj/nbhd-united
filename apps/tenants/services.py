"""Tenant lifecycle services."""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from apps.journal.services import (
    seed_default_documents_for_tenant,
    seed_default_templates_for_tenant,
)

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
    if User.objects.filter(telegram_chat_id=telegram_chat_id).exists():
        raise ValueError(f"Tenant already exists for chat_id={telegram_chat_id}")

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=f"tg_{telegram_chat_id}",
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

            # Seed journal templates for new tenant so daily notes are immediately template-backed.
            seed_default_templates_for_tenant(tenant=tenant)
            seed_default_documents_for_tenant(tenant=tenant)
    except IntegrityError as exc:
        if "telegram_chat_id" in str(exc):
            raise ValueError(f"Tenant already exists for chat_id={telegram_chat_id}") from exc
        raise

    logger.info("Created tenant %s for user %s (chat_id=%s)", tenant.id, user.id, telegram_chat_id)
    return tenant


def reset_daily_counters() -> int:
    """Reset daily message counters. Run via QStash cron at midnight UTC."""
    count = Tenant.objects.filter(messages_today__gt=0).update(messages_today=0)
    logger.info("Reset daily counters for %d tenants", count)
    return count


def reset_monthly_counters() -> int:
    """Reset monthly counters. Run via QStash cron on 1st of month.

    Also clears the quota-email idempotency markers (PR #1.8) so a tenant
    who hit their cap last month can receive a fresh notification chain
    this month. Cleared unconditionally — markers without a corresponding
    elevated cost are harmless (next-month's reconcile cron just resends
    on the next 90% crossing).
    """
    # NOTE: do NOT reset ``purchased_credit`` here — prepaid credit persists
    # across months by design (the included allowance resets; bought credit
    # doesn't). See apps/billing/credits.py + test_credits.MonthlyResetTest.
    # Reset any tenant with a non-zero counter, not just those who sent
    # messages: estimated_cost_this_month accrues on every billable event
    # (e.g. the hourly OpenRouter-spend reconcile cron) regardless of message
    # count, so a cost>0 / messages==0 tenant would otherwise carry last
    # month's cost forward. exclude(all three == 0) == "at least one > 0".
    count = Tenant.objects.exclude(
        messages_this_month=0,
        tokens_this_month=0,
        estimated_cost_this_month=0,
    ).update(
        messages_this_month=0,
        tokens_this_month=0,
        estimated_cost_this_month=0,
    )
    Tenant.objects.update(
        cost_warn_sent_at=None,
        cost_exhausted_email_sent_at=None,
    )
    logger.info("Reset monthly counters for %d tenants", count)
    return count
