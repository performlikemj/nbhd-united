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
SAUTAI_POLL_RECOVERY_STALE_SECONDS = 45
SAUTAI_POLL_RECOVERY_BATCH_SIZE = 200


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


def _sautai_qstash_configured() -> bool:
    return bool(getattr(settings, "QSTASH_TOKEN", "") and getattr(settings, "API_BASE_URL", ""))


def _publish_sautai_poll(job_id, poll_generation: int, *, delay_seconds: int | None = None) -> None:
    from apps.cron.publish import publish_task

    publish_task(
        "generate_sautai_meal_plan",
        str(job_id),
        poll_generation=poll_generation,
        idempotency_key=f"sautai-poll-{job_id.hex}-{poll_generation}",
        delay_seconds=delay_seconds,
    )


def generate_sautai_meal_plan_task(job_id: str, poll_generation: int | None = None) -> None:
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
    from apps.integrations.sautai_client import (
        ASYNC_GENERATION_STATE_KEY,
        SAUTAI_GENERATE_POLL_PENDING,
        SAUTAI_POLL_DELAY_SECONDS,
        async_generation_state,
        call_sautai_generate_plan,
        sautai_poll_generation_filter,
    )

    claimable = SautaiMealPlanJob.objects.filter(id=job_id)
    if poll_generation is None:
        # Initial POST deliveries may reclaim retryable legacy failures, but
        # must never consume an async successor without its generation token.
        claimable = claimable.filter(
            status__in=[SautaiMealPlanJobStatus.PENDING, SautaiMealPlanJobStatus.FAILED]
        ).exclude(result__has_key=ASYNC_GENERATION_STATE_KEY)
    elif isinstance(poll_generation, int) and not isinstance(poll_generation, bool) and poll_generation > 0:
        # The expected generation is part of the database claim. A redelivery
        # that arrives after its successor was persisted can no longer fork the
        # chain, even though the row has returned to PENDING.
        claimable = claimable.filter(
            status=SautaiMealPlanJobStatus.PENDING,
            **sautai_poll_generation_filter(poll_generation),
        )
    else:
        logger.warning(
            "generate_sautai_meal_plan_task: invalid poll generation for job %s",
            str(job_id)[:8],
        )
        return

    claimed = claimable.update(status=SautaiMealPlanJobStatus.GENERATING, error="", updated_at=timezone.now())
    if not claimed:
        logger.info("generate_sautai_meal_plan_task: job %s not claimable — skipping", str(job_id)[:8])
        return

    try:
        job = SautaiMealPlanJob.objects.select_related("tenant__user").get(id=job_id)
    except SautaiMealPlanJob.DoesNotExist:
        logger.warning("generate_sautai_meal_plan_task: job %s not found", str(job_id)[:8])
        return

    action = call_sautai_generate_plan(job, poll_generation=poll_generation)
    if action != SAUTAI_GENERATE_POLL_PENDING:
        return

    state = async_generation_state(job)
    successor_generation = state.get("poll_generation") if isinstance(state, dict) else None
    if not isinstance(successor_generation, int) or isinstance(successor_generation, bool) or successor_generation <= 0:
        SautaiMealPlanJob.objects.filter(id=job.id, status=SautaiMealPlanJobStatus.PENDING).update(
            status=SautaiMealPlanJobStatus.FAILED,
            error="invalid_response: malformed persisted generation state",
            updated_at=timezone.now(),
        )
        return

    # ``publish_task`` executes recursively when QStash is absent. Async poll
    # continuations cannot use that fallback; fail honestly instead of leaving
    # an eternal PENDING row. A transient publish exception is allowed to
    # propagate, and the periodic recovery task advances the token and retries.
    if not _sautai_qstash_configured():
        SautaiMealPlanJob.objects.filter(
            id=job.id,
            status=SautaiMealPlanJobStatus.PENDING,
            **sautai_poll_generation_filter(successor_generation),
        ).update(
            status=SautaiMealPlanJobStatus.FAILED,
            error="sautai_poll_enqueue_unavailable: QStash is not configured",
            updated_at=timezone.now(),
        )
        logger.warning("generate_sautai_meal_plan_task: async poll failed because QStash is not configured")
        return

    _publish_sautai_poll(
        job.id,
        successor_generation,
        delay_seconds=SAUTAI_POLL_DELAY_SECONDS,
    )


def recover_sautai_generation_jobs_task() -> dict[str, int]:
    """Recover dropped async successors and revoke abandoned poll leases.

    The every-minute system cron is a durable backstop for the row-update →
    QStash-publish crash window. Recovery advances ``poll_generation`` with a
    database CAS before publishing, so any delayed old delivery is stale. Jobs
    at or beyond their strict ten-minute deadline are terminalized instead.
    """
    from apps.integrations.models import SautaiMealPlanJob, SautaiMealPlanJobStatus
    from apps.integrations.sautai_client import (
        ASYNC_GENERATION_STATE_KEY,
        SAUTAI_POLL_MAX_ATTEMPTS,
        SAUTAI_POLL_TIMEOUT_ERROR,
        async_generation_state,
        sautai_poll_deadline,
        sautai_poll_generation_filter,
    )

    now = timezone.now()
    jobs = list(
        SautaiMealPlanJob.objects.filter(
            status__in=[SautaiMealPlanJobStatus.PENDING, SautaiMealPlanJobStatus.GENERATING],
            result__has_key=ASYNC_GENERATION_STATE_KEY,
        ).order_by("updated_at")[:SAUTAI_POLL_RECOVERY_BATCH_SIZE]
    )
    counts = {
        "checked": 0,
        "recovered": 0,
        "published": 0,
        "failed": 0,
        "skipped": 0,
        "publish_errors": 0,
    }

    for job in jobs:
        counts["checked"] += 1
        state = async_generation_state(job)
        generation = state.get("poll_generation") if isinstance(state, dict) else None
        attempts = state.get("poll_attempts") if isinstance(state, dict) else None
        deadline = sautai_poll_deadline(state) if isinstance(state, dict) else None
        valid_generation = isinstance(generation, int) and not isinstance(generation, bool) and generation > 0
        valid_attempts = isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0

        if not valid_generation or not valid_attempts or deadline is None:
            updated = SautaiMealPlanJob.objects.filter(
                id=job.id,
                status=job.status,
                result=job.result,
            ).update(
                status=SautaiMealPlanJobStatus.FAILED,
                error="invalid_response: malformed persisted generation state",
                updated_at=now,
            )
            counts["failed" if updated else "skipped"] += 1
            continue

        current = SautaiMealPlanJob.objects.filter(
            id=job.id,
            status=job.status,
            updated_at=job.updated_at,
            **sautai_poll_generation_filter(generation),
        )
        if now >= deadline or attempts >= SAUTAI_POLL_MAX_ATTEMPTS:
            updated = current.update(
                status=SautaiMealPlanJobStatus.FAILED,
                error=SAUTAI_POLL_TIMEOUT_ERROR,
                updated_at=now,
            )
            counts["failed" if updated else "skipped"] += 1
            continue

        age_seconds = (now - job.updated_at).total_seconds()
        if age_seconds < SAUTAI_POLL_RECOVERY_STALE_SECONDS:
            counts["skipped"] += 1
            continue

        if not _sautai_qstash_configured():
            updated = current.update(
                status=SautaiMealPlanJobStatus.FAILED,
                error="sautai_poll_enqueue_unavailable: QStash is not configured",
                updated_at=now,
            )
            counts["failed" if updated else "skipped"] += 1
            continue

        next_state = dict(state)
        next_generation = generation + 1
        next_state["poll_generation"] = next_generation
        updated = current.update(
            result={ASYNC_GENERATION_STATE_KEY: next_state},
            status=SautaiMealPlanJobStatus.PENDING,
            error="",
            updated_at=now,
        )
        if not updated:
            counts["skipped"] += 1
            continue

        counts["recovered"] += 1
        try:
            _publish_sautai_poll(job.id, next_generation)
        except Exception:
            counts["publish_errors"] += 1
            logger.warning("Failed to recover sautai poll successor for job %s", job.id, exc_info=True)
        else:
            counts["published"] += 1

    return counts
