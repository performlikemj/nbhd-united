"""Core (mindfulness) QStash task handlers.

Tasks load their subject by id (QStash-body-safe) and re-import collaborators
locally so ``unittest.mock.patch`` targets resolve (the load-bearing local
re-import pattern used across tasks modules).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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
    """Render a pending MeditationSession by id (async via QStash)."""
    from apps.core.models import MeditationSession, MeditationStatus
    from apps.core.services import render_meditation

    try:
        session = MeditationSession.objects.get(id=meditation_id)
    except MeditationSession.DoesNotExist:
        logger.warning("render_meditation_task: session %s not found", str(meditation_id)[:8])
        return

    if session.status not in (MeditationStatus.PENDING, MeditationStatus.FAILED):
        logger.info(
            "render_meditation_task: session %s already %s — skipping",
            str(meditation_id)[:8],
            session.status,
        )
        return

    render_meditation(session)
