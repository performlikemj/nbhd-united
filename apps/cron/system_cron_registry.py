"""Shared QStash system-cron registration core.

Extracted from the ``register_system_crons`` view so the exact same
register / update / deregister logic runs from two entry points:

* the ``X-Deploy-Secret``-authed CI view (``apps/cron/views.py``), fired once
  per deploy from ci-cd.yml, and
* a daily QStash-signed ``reconcile_system_crons`` task, so any registration
  drift self-heals within 24h.

Why the daily reconcile exists (incident 2026-07-09b): the post-deploy
register call fired ~18s after the revision was created and hit the OLD
revision's code, which answered ``/health/`` 200 instantly but still ran the
pre-retirement ``SYSTEM_CRONS`` — so a retired schedule was never swapped and
kept firing, silently. The build-identity health gate now blocks that specific
race at deploy time, but a daily belt-and-braces reconcile guarantees
convergence even if a future post-deploy call races a stale revision, 5xxs into
the DLQ, or is skipped.

``SYSTEM_CRONS`` / ``RETIRED_CRON_PATHS`` stay the single source of truth in
``apps/cron/management/commands/register_system_crons.py``; every entry point
imports them from there.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

QSTASH_SCHEDULES_URL = "https://qstash.upstash.io/v2/schedules"


class SystemCronConfigError(RuntimeError):
    """Raised when QSTASH_TOKEN is unset — cannot talk to QStash at all."""


def sync_system_crons(base_url: str) -> dict:
    """Register / update / deregister the QStash system-cron schedules.

    Idempotent: a schedule already at its desired cron is left alone; a
    changed cron expr is delete-then-recreate; any live schedule at a
    ``RETIRED_CRON_PATHS`` destination is deleted so a retirement actually
    stops firing (the register loop only ADDs/UPDATEs entries still present).

    ``base_url`` is the deployed Django origin; a trailing slash is stripped
    here. Returns a result dict of lists:
    ``{registered, updated, skipped, failed, deregistered}``.

    Raises ``SystemCronConfigError`` if QSTASH_TOKEN is unset. Lets ``httpx``
    errors from the initial schedules fetch propagate — callers decide how to
    surface them (the view as a 500, the task via logging).
    """
    import httpx

    from apps.cron.management.commands.register_system_crons import (
        RETIRED_CRON_PATHS,
        SYSTEM_CRONS,
    )

    qstash_token = getattr(settings, "QSTASH_TOKEN", "")
    if not qstash_token:
        raise SystemCronConfigError("QSTASH_TOKEN not configured")

    base_url = base_url.rstrip("/")

    headers = {
        "Authorization": f"Bearer {qstash_token}",
        "Content-Type": "application/json",
    }

    resp = httpx.get(QSTASH_SCHEDULES_URL, headers=headers)
    resp.raise_for_status()
    existing = {s["destination"]: s for s in resp.json()}

    registered: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for name, cron_expr, path in SYSTEM_CRONS:
        destination = f"{base_url}{path}"
        if destination in existing:
            existing_sched = existing[destination]
            if existing_sched.get("cron") == cron_expr:
                skipped.append(name)
                continue

            # Cron expression changed — delete old and recreate.
            schedule_id = existing_sched.get("scheduleId")
            if not schedule_id:
                skipped.append(name)
                continue

            del_resp = httpx.delete(
                f"{QSTASH_SCHEDULES_URL}/{schedule_id}",
                headers=headers,
            )
            if del_resp.status_code not in (200, 204):
                logger.error(
                    "Failed to delete old schedule %s: %s %s",
                    name,
                    del_resp.status_code,
                    del_resp.text,
                )
                failed.append(name)
                continue

            create_resp = httpx.post(
                f"{QSTASH_SCHEDULES_URL}/{destination}",
                headers={**headers, "Upstash-Cron": cron_expr},
            )
            if create_resp.status_code in (200, 201):
                updated.append(name)
                logger.info("Updated QStash cron: %s → %s", name, cron_expr)
            else:
                failed.append(name)
                logger.error(
                    "Failed to recreate QStash cron %s: %s %s",
                    name,
                    create_resp.status_code,
                    create_resp.text,
                )
            continue

        create_resp = httpx.post(
            f"{QSTASH_SCHEDULES_URL}/{destination}",
            headers={**headers, "Upstash-Cron": cron_expr},
        )
        if create_resp.status_code in (200, 201):
            registered.append(name)
            logger.info("Registered QStash cron: %s → %s", name, cron_expr)
        else:
            failed.append(name)
            logger.error(
                "Failed to register QStash cron %s: %s %s",
                name,
                create_resp.status_code,
                create_resp.text,
            )

    # Deregister retired crons — delete any live schedule at a retired path so a
    # cron dropped from SYSTEM_CRONS actually stops firing. See RETIRED_CRON_PATHS.
    deregistered: list[str] = []
    for path in RETIRED_CRON_PATHS:
        destination = f"{base_url}{path}"
        existing_sched = existing.get(destination)
        if not existing_sched:
            continue
        schedule_id = existing_sched.get("scheduleId")
        if not schedule_id:
            continue
        del_resp = httpx.delete(
            f"{QSTASH_SCHEDULES_URL}/{schedule_id}",
            headers=headers,
        )
        if del_resp.status_code in (200, 204):
            deregistered.append(path)
            logger.info("Deregistered retired QStash cron: %s", path)
        else:
            failed.append(path)
            logger.error(
                "Failed to deregister retired QStash cron %s: %s %s",
                path,
                del_resp.status_code,
                del_resp.text,
            )

    return {
        "registered": registered,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "deregistered": deregistered,
    }


def reconcile_system_crons_task() -> dict:
    """Daily QStash-signed reconcile of the system-cron schedules.

    Belt-and-braces for incident 2026-07-09b: re-runs ``sync_system_crons`` so
    any registration drift the post-deploy call missed (it raced a stale
    revision, 5xx'd into the DLQ, or was skipped) self-heals within 24h.

    Reads ``base_url`` from ``settings.DJANGO_BASE_URL``. Fails gracefully —
    logs an error and returns without raising — when it is unset, so a QStash
    fire never lands in the DLQ over a control-plane config gap.
    """
    base_url = (getattr(settings, "DJANGO_BASE_URL", "") or "").rstrip("/")
    if not base_url:
        logger.error(
            "reconcile_system_crons: DJANGO_BASE_URL unset — cannot reconcile "
            "system crons; set it on the control plane so daily drift-repair runs."
        )
        return {"status": "skipped", "reason": "DJANGO_BASE_URL unset"}

    result = sync_system_crons(base_url)
    logger.info("reconcile_system_crons: %s", result)
    return result
