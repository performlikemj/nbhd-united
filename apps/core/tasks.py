"""Core (mindfulness) QStash task handlers.

Tasks load their subject by id (QStash-body-safe) and re-import collaborators
locally so ``unittest.mock.patch`` targets resolve (the load-bearing local
re-import pattern used across tasks modules).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MEDITATION_REAP_AGE_MINUTES = 10
_MEDITATION_REAP_LIMIT = 50


def schedule_core_welcome_task(tenant_id: str) -> None:
    """Schedule the Core welcome cron (~90s post-restart). Fire-and-forget."""
    from apps.core.views import _schedule_core_welcome
    from apps.tenants.models import Tenant

    try:
        tenant = Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist:
        logger.warning("schedule_core_welcome_task: tenant %s not found", str(tenant_id)[:8])
        return
    try:
        _schedule_core_welcome(tenant)
    except Exception:
        logger.warning("schedule_core_welcome_task failed for %s", str(tenant_id)[:8], exc_info=True)


def render_meditation_task(meditation_id: str) -> None:
    """Resolve a MeditationSession and let the service decide claimability."""
    from apps.core.models import MeditationSession
    from apps.core.services import render_meditation

    try:
        session = MeditationSession.objects.get(id=meditation_id)
    except MeditationSession.DoesNotExist:
        logger.warning("render_meditation_task: session %s not found", str(meditation_id)[:8])
        return

    render_meditation(session)


def compose_meditation_task(meditation_id: str) -> None:
    """Author a pending session's manifest, then enqueue its render via QStash.

    The web orb's entry point: the consumer view creates a PENDING session and
    enqueues this. Only acts on a PENDING row, so a retry before render claims the
    row can resume from the persisted manifest; after the render begins it is a
    no-op (render_meditation's own claim guards double-render).
    """
    from apps.core.models import MeditationSession, MeditationStatus
    from apps.core.services import compose_meditation

    try:
        session = MeditationSession.objects.get(id=meditation_id)
    except MeditationSession.DoesNotExist:
        logger.warning("compose_meditation_task: session %s not found", str(meditation_id)[:8])
        return

    if session.status != MeditationStatus.PENDING:
        logger.info(
            "compose_meditation_task: session %s not pending (%s) — skipping",
            str(meditation_id)[:8],
            session.status,
        )
        return

    compose_meditation(session)


def reap_meditations() -> dict[str, int]:
    """Republish bounded recovery work for stranded meditation sessions.

    The reaper never renders or changes session state itself. It only republishes
    the appropriate QStash task, leaving the render service's atomic claim as the
    single authority over retries and stale workers. Publishing stale snapshots is
    safe: a live worker or a concurrently completed row will fail the service
    claim (and the compose task retains its own PENDING guard).

    This zero-argument task is registered for a QStash cron, but the schedule is
    provisioned separately by the orchestrator.
    """
    from datetime import timedelta

    from django.conf import settings
    from django.db.models import Q
    from django.utils import timezone

    from apps.core import render
    from apps.core.models import MeditationSession, MeditationStatus
    from apps.cron.publish import publish_task

    now = timezone.now()
    retry_cutoff = now - timedelta(minutes=_MEDITATION_REAP_AGE_MINUTES)
    stale_minutes = int(getattr(settings, "CORE_RENDER_STALE_MINUTES", 15) or 15)
    stale_cutoff = now - timedelta(minutes=stale_minutes)
    max_attempts = int(getattr(settings, "CORE_RENDER_MAX_ATTEMPTS", 3) or 3)

    candidates = list(
        MeditationSession.objects.filter(
            Q(
                status=MeditationStatus.RENDERING,
                updated_at__lt=stale_cutoff,
            )
            | Q(
                status=MeditationStatus.PENDING,
                updated_at__lt=retry_cutoff,
            )
            | Q(
                status=MeditationStatus.FAILED,
                failure_class="transient",
                attempt_count__lt=max_attempts,
                updated_at__lt=retry_cutoff,
            )
        )
        .exclude(failure_class="terminal")
        .only("id", "status", "manifest", "updated_at")
        .order_by("updated_at", "id")[:_MEDITATION_REAP_LIMIT]
    )

    render_published = 0
    compose_published = 0
    errors = 0
    for session in candidates:
        task_name = "render_meditation"
        if session.status == MeditationStatus.PENDING and render.validate_manifest(session.manifest):
            task_name = "compose_meditation"

        try:
            publish_task(task_name, str(session.id))
        except Exception:
            logger.exception(
                "reap_meditations: failed to publish %s for session %s",
                task_name,
                str(session.id)[:8],
            )
            errors += 1
            continue

        if task_name == "render_meditation":
            render_published += 1
        else:
            compose_published += 1

    if candidates:
        logger.warning(
            "reap_meditations: %d candidate(s), %d render published, %d compose published, %d errors",
            len(candidates),
            render_published,
            compose_published,
            errors,
        )

    return {
        "candidates": len(candidates),
        "render_published": render_published,
        "compose_published": compose_published,
        "errors": errors,
    }
