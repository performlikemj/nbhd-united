"""Tenant lifecycle services."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from apps.journal.services import (
    seed_default_documents_for_tenant,
    seed_default_templates_for_tenant,
)

from .models import Tenant, User

logger = logging.getLogger(__name__)

# One month (30 days) gives users time to build a working relationship with the assistant.
TRIAL_DAYS = 30


def prepare_tenant_provisioning(user: User) -> tuple[Tenant, bool]:
    """Idempotently create the durable trial-tenant row for ``user``.

    External publication is deliberately separate so auth callers can commit
    this repairable state before starting any network work.
    """
    existing = Tenant.objects.filter(user=user).first()
    if existing is not None:
        return existing, False

    now = timezone.now()
    try:
        with transaction.atomic():
            tenant = Tenant.objects.create(
                user=user,
                is_trial=True,
                trial_started_at=now,
                trial_ends_at=now + timedelta(days=TRIAL_DAYS),
                model_tier=Tenant.ModelTier.STARTER,
                status=Tenant.Status.PROVISIONING,
            )
    except IntegrityError:
        # A concurrent caller won the OneToOne(user) race — return their row.
        return Tenant.objects.get(user=user), False

    logger.info(
        "tenant_provisioning tenant_id=%s user_id=%s stage=tenant_created error=",
        tenant.id,
        user.id,
    )
    seed_default_templates_for_tenant(tenant=tenant)
    return tenant, True


def _mark_provisioning_pending(tenant_id: str) -> None:
    Tenant.objects.filter(id=tenant_id).update(
        status=Tenant.Status.PENDING,
        updated_at=timezone.now(),
    )


def _publish_tenant_provisioning(tenant_id: str, user_id: str) -> bool:
    from apps.cron.publish import publish_task

    try:
        publish_task("provision_tenant", tenant_id)
        logger.info(
            "tenant_provisioning tenant_id=%s user_id=%s stage=publish_provision_task error=",
            tenant_id,
            user_id,
        )
        return True
    except Exception as exc:
        try:
            _mark_provisioning_pending(tenant_id)
        except Exception:
            logger.exception(
                "tenant_provisioning tenant_id=%s user_id=%s stage=publish_failure_pending_mark_failed error=",
                tenant_id,
                user_id,
            )
        logger.exception(
            "tenant_provisioning tenant_id=%s user_id=%s stage=publish_provision_task_failed error=%s",
            tenant_id,
            user_id,
            exc,
        )
        return False


def _publish_tenant_provisioning_in_thread(tenant_id: str, user_id: str) -> None:
    try:
        _publish_tenant_provisioning(tenant_id, user_id)
    finally:
        # A one-shot daemon thread must not pin a database pool slot after it
        # finishes marking a failed publish PENDING.
        close_old_connections()


def kickoff_tenant_provisioning(
    tenant_id: str,
    user_id: str,
    *,
    force_background: bool = False,
) -> bool:
    """Start the publish without allowing kickoff failures to escape.

    Production QStash calls run on a daemon thread so request handlers never
    wait on the external publish. The no-QStash development/test fallback stays
    synchronous unless ``force_background`` is requested by an auth path whose
    latency contract requires it.
    """
    run_in_background = force_background or (
        bool(getattr(settings, "QSTASH_TOKEN", "")) and not getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False)
    )
    if not run_in_background:
        return _publish_tenant_provisioning(tenant_id, user_id)

    try:
        threading.Thread(
            target=_publish_tenant_provisioning_in_thread,
            args=(tenant_id, user_id),
            daemon=True,
            name=f"tenant-provision-{tenant_id[:8]}",
        ).start()
        return True
    except Exception as exc:
        try:
            _mark_provisioning_pending(tenant_id)
        except Exception:
            logger.exception(
                "tenant_provisioning tenant_id=%s user_id=%s stage=provision_kickoff_pending_mark_failed error=",
                tenant_id,
                user_id,
            )
        logger.exception(
            "tenant_provisioning tenant_id=%s user_id=%s stage=provision_kickoff_failed error=%s",
            tenant_id,
            user_id,
            exc,
        )
        return False


def ensure_tenant_provisioned(user: User) -> tuple[Tenant, bool, bool]:
    """Idempotently create + kick off provisioning of a trial tenant for ``user``.

    This is the single source of truth for "a brand-new user gets a backend
    workspace". Every post-authentication chokepoint routes through it —
    web onboarding (``OnboardTenantView``) and the iOS web-signup PKCE handoff
    (``ExchangeView``) — so no path can leave an authenticated user stranded
    without a tenant (the bug that 404'd every feature tab for handoff users).

    Returns ``(tenant, created, provision_kicked_off)``:

    * ``created`` is ``False`` when the user already had a tenant (pure no-op —
      safe to call on every sign-in).
    * ``provision_kicked_off`` is ``False`` when the publish failed in the
      synchronous fallback or a background thread could not start. The tenant
      is left at ``PENDING`` for ``repair-stale-provisioning``. In production,
      a successfully started background publish returns ``True`` immediately;
      a later publish failure marks the row ``PENDING`` from that thread.
    """
    tenant, created = prepare_tenant_provisioning(user)
    if not created:
        return tenant, False, True

    kicked_off = kickoff_tenant_provisioning(str(tenant.id), str(user.id))
    if not kicked_off:
        tenant.refresh_from_db(fields=["status", "updated_at"])
    return tenant, True, kicked_off


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
