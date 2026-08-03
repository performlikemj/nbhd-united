"""Scheduled integration maintenance tasks."""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import Integration
from .services import (
    COMPOSIO_MANAGED_PROVIDERS,
    M2M_PROVIDERS,
    get_provider_client_credentials,
    load_tokens_from_key_vault,
    refresh_integration_tokens,
)

logger = logging.getLogger(__name__)

REFRESH_LEAD_MINUTES = 15


def refresh_expiring_integrations_task() -> dict[str, int]:
    """Refresh integrations that are close to expiring.

    Intended to be triggered by QStash on a recurring cadence.
    """
    threshold = timezone.now() + timedelta(minutes=REFRESH_LEAD_MINUTES)
    integrations = (
        Integration.objects.select_related("tenant")
        .filter(
            status=Integration.Status.ACTIVE,
        )
        .exclude(provider__in=COMPOSIO_MANAGED_PROVIDERS | M2M_PROVIDERS)
        .filter(Q(token_expires_at__isnull=True) | Q(token_expires_at__lte=threshold))
    )

    checked = refreshed = expired = errored = 0

    for integration in integrations:
        checked += 1
        client_id, client_secret = get_provider_client_credentials(integration.provider)
        if not client_id or not client_secret:
            integration.status = Integration.Status.ERROR
            integration.save(update_fields=["status", "updated_at"])
            errored += 1
            logger.warning(
                "Skipping refresh for %s/%s due to missing client credentials",
                integration.tenant_id,
                integration.provider,
            )
            continue

        raw_tokens = load_tokens_from_key_vault(integration.tenant, integration.provider)
        tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            integration.status = Integration.Status.EXPIRED
            integration.save(update_fields=["status", "updated_at"])
            expired += 1
            logger.warning(
                "Integration missing refresh token; marking expired for %s/%s",
                integration.tenant_id,
                integration.provider,
            )
            continue

        try:
            refresh_integration_tokens(
                tenant=integration.tenant,
                provider=integration.provider,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
            )
            refreshed += 1
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            integration.status = Integration.Status.EXPIRED if status_code in (400, 401) else Integration.Status.ERROR
            integration.save(update_fields=["status", "updated_at"])
            if integration.status == Integration.Status.EXPIRED:
                expired += 1
            else:
                errored += 1
            logger.warning(
                "Refresh failed for %s/%s with status %s",
                integration.tenant_id,
                integration.provider,
                status_code,
            )
        except Exception:
            integration.status = Integration.Status.ERROR
            integration.save(update_fields=["status", "updated_at"])
            errored += 1
            logger.exception(
                "Unexpected refresh failure for %s/%s",
                integration.tenant_id,
                integration.provider,
            )

    return {
        "checked": checked,
        "refreshed": refreshed,
        "expired": expired,
        "errored": errored,
    }


def generate_sautai_meal_plan_task(job_id: str) -> None:
    """Advance one POST/poll/finalize step for a SautaiMealPlanJob.

    Idempotency is an ATOMIC claim, not a read-then-check: a PENDING/FAILED
    row transitions to GENERATING in one UPDATE (mirrors
    ``apps.core.services.render_meditation``'s compare-and-swap). Zero rows
    updated means a concurrent QStash delivery already owns this job —
    skip cleanly rather than re-reading the row and racing a second HTTP
    call to sautai / a second completion notify. sautai's own
    ``create_meal_plan_for_user()`` is idempotent per (user, week) too, but
    that's a second line of defense, not a substitute for claiming first.

    A sautai ``202`` or active status read returns the row to PENDING, then this
    task publishes a distinct delivery delayed by 15 seconds. This is a
    re-enqueue state machine, not a blocking sleep loop. POST transport/5xx
    failures still raise ``RetryableSautaiError`` for QStash redelivery; terminal
    failures return normally.
    """
    from apps.integrations.models import SautaiMealPlanJob, SautaiMealPlanJobStatus

    claimed = SautaiMealPlanJob.objects.filter(
        id=job_id,
        status__in=[SautaiMealPlanJobStatus.PENDING, SautaiMealPlanJobStatus.FAILED],
    ).update(status=SautaiMealPlanJobStatus.GENERATING, error="", updated_at=timezone.now())
    if not claimed:
        logger.info("generate_sautai_meal_plan_task: job %s not claimable — skipping", str(job_id)[:8])
        return

    try:
        job = SautaiMealPlanJob.objects.select_related("tenant__user").get(id=job_id)
    except SautaiMealPlanJob.DoesNotExist:
        logger.warning("generate_sautai_meal_plan_task: job %s not found", str(job_id)[:8])
        return

    from apps.integrations.sautai_client import (
        ASYNC_GENERATION_STATE_KEY,
        SAUTAI_GENERATE_POLL_PENDING,
        SAUTAI_POLL_DELAY_SECONDS,
        call_sautai_generate_plan,
    )

    action = call_sautai_generate_plan(job)
    if action != SAUTAI_GENERATE_POLL_PENDING:
        return

    # ``publish_task`` deliberately executes synchronously when QStash is not
    # configured. That fallback would turn async polling into a tight recursive
    # loop holding this worker, so leave the durable row PENDING instead. A real
    # async deployment always carries both settings, and the runtime's stale-row
    # recovery remains available if publishing is temporarily unavailable.
    if not getattr(settings, "QSTASH_TOKEN", "") or not getattr(settings, "API_BASE_URL", ""):
        logger.warning("generate_sautai_meal_plan_task: async poll pending but QStash is not configured")
        return

    state = job.result.get(ASYNC_GENERATION_STATE_KEY, {}) if isinstance(job.result, dict) else {}
    attempt = state.get("poll_attempts", 0) if isinstance(state, dict) else 0
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        attempt = 0

    from apps.cron.publish import publish_task

    publish_task(
        "generate_sautai_meal_plan",
        str(job.id),
        idempotency_key=f"sautai-poll-{job.id.hex}-{attempt}",
        delay_seconds=SAUTAI_POLL_DELAY_SECONDS,
    )
