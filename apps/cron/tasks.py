"""Cron lifecycle maintenance tasks (QStash-scheduled — never Celery).

Registered in ``TASK_MAP`` (apps/cron/views.py) and fired by a no-body QStash
publish to ``/api/cron/trigger/<name>/``.
"""

from __future__ import annotations

import logging
from datetime import UTC, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.cron.models import CronJob

logger = logging.getLogger(__name__)

APPLE_REVOCATION_MAX_DECRYPT_ATTEMPTS = 3


def _record_apple_revocation_error(outbox_id, message: str) -> None:
    from apps.tenants.apple_models import AppleRevocationOutbox

    AppleRevocationOutbox.objects.filter(id=outbox_id, revoked_at__isnull=True).update(
        last_error=message[:512],
    )


def revoke_apple_token_task(outbox_id: str) -> dict:
    """Idempotently revoke one durable Apple refresh-token grant."""

    from django.db import transaction
    from django.utils import timezone

    from apps.tenants.apple_client import AppleUnavailable, revoke_apple_refresh_token
    from apps.tenants.apple_crypto import AppleTokenCryptoError, decrypt_apple_refresh_token
    from apps.tenants.apple_models import AppleRevocationOutbox

    with transaction.atomic():
        try:
            row = AppleRevocationOutbox.objects.select_for_update(of=("self",)).get(id=outbox_id)
        except (AppleRevocationOutbox.DoesNotExist, ValueError):
            return {"status": "missing"}
        if row.revoked_at is not None:
            return {"status": "already_revoked"}
        if row.last_error.startswith("terminal:"):
            return {
                "status": "terminal",
                "reason": row.last_error.removeprefix("terminal:"),
            }
        row.attempts += 1
        row.last_attempt_at = timezone.now()
        row.save(update_fields=["attempts", "last_attempt_at"])
        attempts = row.attempts
        ciphertext = row.token_ciphertext

    # Decrypt and Apple HTTP both happen after the short claim transaction.
    try:
        refresh_token = decrypt_apple_refresh_token(ciphertext)
    except AppleTokenCryptoError as exc:
        terminal = attempts >= APPLE_REVOCATION_MAX_DECRYPT_ATTEMPTS
        prefix = "terminal:" if terminal else "retry:"
        _record_apple_revocation_error(outbox_id, f"{prefix}decrypt_failed")
        logger.warning(
            "auth.apple.revocation.decrypt_failed outbox_id=%s attempt=%s terminal=%s",
            outbox_id,
            attempts,
            terminal,
        )
        if terminal:
            return {"status": "terminal", "reason": "decrypt_failed"}
        raise RuntimeError("Apple revocation token decrypt failed") from exc

    try:
        response_status = revoke_apple_refresh_token(refresh_token)
    except AppleUnavailable as exc:
        _record_apple_revocation_error(outbox_id, f"retry:{exc.reason}")
        logger.warning(
            "auth.apple.revocation.transport_failed outbox_id=%s attempt=%s reason=%s",
            outbox_id,
            attempts,
            exc.reason,
        )
        raise
    except Exception as exc:
        reason = f"retry:client_error_{type(exc).__name__}"
        _record_apple_revocation_error(outbox_id, reason)
        logger.warning(
            "auth.apple.revocation.client_failed outbox_id=%s attempt=%s",
            outbox_id,
            attempts,
        )
        raise

    now = timezone.now()
    if 400 <= response_status < 500:
        note = f"apple_{response_status}_treated_as_revoked"
    else:
        note = ""
    AppleRevocationOutbox.objects.filter(id=outbox_id, revoked_at__isnull=True).update(
        revoked_at=now,
        last_error=note,
    )
    logger.info(
        "auth.apple.revocation.complete outbox_id=%s outcome=%s",
        outbox_id,
        "apple_4xx" if note else "revoked",
    )
    return {"status": "revoked", "apple_status": response_status}


# How long past its fire time a one-shot cron is left alone before being retired.
# The container owns firing; this row is bookkeeping. A tenant hibernated across
# the fire time may run the job late on wake, so a generous grace keeps a
# same-hour wake honest without leaving the name squatted for long.
AT_CRON_GRACE = timedelta(hours=1)


def expire_finished_at_crons_task() -> dict:
    """Retire one-shot ("at") crons whose fire time has passed.

    WHY THIS EXISTS: OpenClaw deletes an at-kind job from its own store when it
    fires (``deleteAfterRun``), but nothing tells Django — there is **no**
    container→control-plane feedback path for cron fires (the ``cron_changed``
    hook is an in-container cache manager and makes zero Django calls). So the
    Postgres row stayed ``enabled=True`` forever. Combined with what used to be
    an UNCONDITIONAL ``(tenant, name)`` uniqueness constraint, the name was
    squatted for good: a user asking for the same reminder twice got a 409 on the
    second ask ("remind me at 3pm to call Mom" worked once, never again).

    The constraint is now scoped to ``enabled=True`` (apps/cron/models.py), so
    retiring the row here is what actually frees the name. This sweep IS the
    backfill — the first run retires every already-spent row.

    Zero-arg by the QStash TASK_MAP contract. Idempotent: a second pass over the
    same rows matches nothing, because they are already disabled.

    NOT a container operation. The bulk ``.update()`` below is deliberate, not a
    micro-optimization:
      * a per-row ``.save()`` would fire the ``pre_save`` contract-baking signal
        and re-render ``data`` on a dead row, and ``post_save`` would schedule a
        push to the container;
      * there is nothing to push — the container already deleted its copy when the
        job fired. This UPDATE RE-SYNCS the mirror rather than desyncing it.
        (invariants §9 is about deleting rows the container still HOLDS.)
    """
    now = timezone.now()
    cutoff = now - AT_CRON_GRACE

    # The ``kind == "at"`` gate below is the ONLY discriminator, and it is sufficient:
    # a recurring cron has kind "cron"/"every", never "at".
    #
    # Deliberately NOT filtered on ``managed=False``, even though that is what
    # create_typed_cron / create_freeform_cron stamp on an at-kind schedule
    # (services.py::_is_at_schedule). ``upsert_jobs_to_cache`` (apps/cron/cache.py)
    # mirrors gateway jobs into Postgres WITHOUT passing ``managed``, so it takes the
    # model default of True — meaning a one-shot that got mirrored by a console open is
    # ``managed=True`` and a managed-only filter would skip it forever, leaving it to
    # squat its name with no retirement path. Filtering on ``managed`` would reintroduce
    # the very bug this task exists to fix, for a row shape that is reachable today (the
    # agent's raw ``cron`` tool is still on every tenant; the deny-list cutover has not
    # happened).
    #
    # Nor on creation_path: a freeform at-cron squats its name identically.
    candidates = CronJob.objects.filter(enabled=True).values_list("id", "data")

    expired_ids: list[int] = []
    for cron_id, data in candidates:
        schedule = (data or {}).get("schedule")
        if not isinstance(schedule, dict) or schedule.get("kind") != "at":
            continue
        fire_at = parse_datetime(str(schedule.get("at") or ""))
        if fire_at is None:
            # A malformed schedule is a finding, not a crash. Leave the row alone
            # and say so, so a broken writer stays visible instead of being
            # silently retired.
            logger.warning(
                "expire_finished_at_crons: cron %s has a missing/unparseable 'at' schedule — skipped",
                cron_id,
            )
            continue
        if timezone.is_naive(fire_at):
            fire_at = fire_at.replace(tzinfo=UTC)
        if fire_at < cutoff:
            expired_ids.append(cron_id)

    if not expired_ids:
        logger.info("expire_finished_at_crons: nothing to retire")
        return {"expired": 0, "ids": []}

    # ``updated_at`` is stamped explicitly: .update() bypasses auto_now along with the
    # signals, and "when was this retired?" is the first forensic question anyone asks
    # of a disabled cron.
    expired = CronJob.objects.filter(id__in=expired_ids, enabled=True).update(enabled=False, updated_at=now)
    logger.info("expire_finished_at_crons: retired %s spent at-cron(s) %s", expired, expired_ids)
    return {"expired": expired, "ids": expired_ids}
