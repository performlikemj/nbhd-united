"""Cron lifecycle maintenance tasks (QStash-scheduled — never Celery).

Registered in ``TASK_MAP`` (apps/cron/views.py) and fired by a no-body QStash
publish to ``/api/cron/trigger/<name>/``.
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import UTC, timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.cron.models import CronJob, CronJobSource

logger = logging.getLogger(__name__)

APPLE_REVOCATION_MAX_DECRYPT_ATTEMPTS = 5
APPLE_REVOCATION_DECRYPT_FAILURE_WINDOW = timedelta(hours=24)
APPLE_REVOCATION_BATCH_SIZE = 10
APPLE_REVOCATION_LEASE = timedelta(minutes=10)
APPLE_REVOCATION_INVALID_CLIENT_TERMINAL = 5


def _record_apple_revocation_error(
    outbox_id,
    claimed_attempt: int,
    message: str,
) -> int:
    from apps.tenants.apple_models import AppleRevocationOutbox

    return (
        AppleRevocationOutbox.objects.filter(
            id=outbox_id,
            attempts=claimed_attempt,
            revoked_at__isnull=True,
        )
        .exclude(last_error__startswith="terminal:")
        .update(last_error=message[:512])
    )


def revoke_apple_token_task(outbox_id: str) -> dict:
    """Idempotently run the leased worker for one published outbox UUID."""

    from apps.tenants.apple_models import AppleRevocationOutbox

    try:
        parsed_outbox_id = uuid.UUID(str(outbox_id))
    except ValueError:
        return {"status": "missing"}
    results = process_apple_revocation_outbox((parsed_outbox_id,))
    if results:
        result = dict(results[0])
        result.pop("outbox_id", None)
        return result

    row = AppleRevocationOutbox.objects.filter(id=parsed_outbox_id).first()
    if row is None:
        return {"status": "missing"}
    if row.revoked_at is not None:
        return {"status": "already_revoked"}
    if row.last_error.startswith("terminal:"):
        return {
            "status": "terminal",
            "reason": row.last_error.removeprefix("terminal:"),
        }
    return {"status": "deferred"}


def _apple_retry_at(now, attempts: int):
    delay = min(86400, 60 * (2 ** min(attempts, 10)))
    return now + timedelta(seconds=delay + random.uniform(0, 30))


def process_apple_revocation_outbox(outbox_ids=None) -> list[dict]:
    """Claim at most ten due rows, call Apple unlocked, and CAS results."""

    from django.db import transaction
    from django.db.models import Q

    from apps.tenants.apple_client import AppleUnavailable, revoke_apple_refresh_token
    from apps.tenants.apple_crypto import (
        AppleTokenCryptoError,
        decrypt_apple_refresh_token,
        validate_apple_token_keyring,
    )
    from apps.tenants.apple_models import AppleRevocationOutbox

    if not validate_apple_token_keyring():
        logger.error("auth.apple.revocation.configuration_invalid reason=invalid_token_keyring")
        return []

    now = timezone.now()
    due = (
        Q(revoked_at__isnull=True)
        & ~Q(last_error__startswith="terminal:")
        & (Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        & (Q(claimed_at__isnull=True) | Q(claimed_at__lte=now - APPLE_REVOCATION_LEASE))
    )
    if outbox_ids is not None:
        due &= Q(id__in=outbox_ids)

    claimed: list[tuple] = []
    results: list[dict] = []
    with transaction.atomic():
        rows = list(
            AppleRevocationOutbox.objects.select_for_update(skip_locked=True)
            .filter(due)
            .order_by("next_attempt_at", "created_at", "id")[:APPLE_REVOCATION_BATCH_SIZE]
        )
        for row in rows:
            claim_time = timezone.now()
            defaulted_client_id = row.client_id is None
            if defaulted_client_id:
                row.client_id = settings.APPLE_SIWA_SERVICES_ID
                row.backfill_source = "worker_default"
            try:
                refresh_token = decrypt_apple_refresh_token(row.token_ciphertext)
            except AppleTokenCryptoError:
                attempts = row.attempts + 1
                continuing_failure = (
                    row.attempts > 0 and row.last_error.endswith(":decrypt_failed") and row.last_attempt_at is not None
                )
                first_failure_at = row.last_attempt_at if continuing_failure else claim_time
                terminal = (
                    attempts >= APPLE_REVOCATION_MAX_DECRYPT_ATTEMPTS
                    and claim_time - first_failure_at >= APPLE_REVOCATION_DECRYPT_FAILURE_WINDOW
                )
                row.attempts = attempts
                # For decrypt failures, last_attempt_at intentionally retains the
                # first failure time so terminalization proves a 24-hour span.
                # Operator recovery: clear last_error and reset attempts to requeue.
                row.last_attempt_at = first_failure_at
                row.claimed_at = None
                row.consecutive_invalid_client = 0
                row.last_error = f"{'terminal' if terminal else 'retry'}:decrypt_failed"
                row.next_attempt_at = None if terminal else _apple_retry_at(claim_time, attempts)
                update_fields = [
                    "attempts",
                    "last_attempt_at",
                    "claimed_at",
                    "consecutive_invalid_client",
                    "last_error",
                    "next_attempt_at",
                ]
                if defaulted_client_id:
                    update_fields.extend(["client_id", "backfill_source"])
                row.save(update_fields=update_fields)
                logger.error(
                    "auth.apple.revocation.decrypt_failed outbox_id=%s attempt=%s terminal=%s",
                    row.id,
                    attempts,
                    terminal,
                )
                results.append(
                    {
                        "outbox_id": str(row.id),
                        "status": "terminal" if terminal else "retry",
                        "reason": "decrypt_failed",
                    }
                )
                continue
            row.claimed_at = claim_time
            update_fields = ["claimed_at"]
            if defaulted_client_id:
                update_fields.extend(["client_id", "backfill_source"])
            row.save(update_fields=update_fields)
            claimed.append(
                (
                    row.id,
                    claim_time,
                    row.client_id,
                    refresh_token,
                    row.attempts,
                    row.consecutive_invalid_client,
                )
            )

    for row_id, claim_time, client_id, refresh_token, prior_attempts, prior_invalid_client in claimed:
        response_status = None
        failure_reason = ""
        try:
            response_status = revoke_apple_refresh_token(
                refresh_token,
                client_id=client_id,
            )
        except AppleUnavailable as exc:
            failure_reason = exc.reason
        except Exception as exc:  # noqa: BLE001 - persist provider/client failures uniformly
            failure_reason = f"client_error_{type(exc).__name__}"

        completed_at = timezone.now()
        attempts = prior_attempts + 1
        updates = {
            "attempts": attempts,
            "last_attempt_at": completed_at,
            "claimed_at": None,
        }
        if response_status is not None:
            note = f"apple_{response_status}_treated_as_revoked" if 400 <= response_status < 500 else ""
            updates.update(
                revoked_at=completed_at,
                next_attempt_at=None,
                last_error=note,
                consecutive_invalid_client=0,
            )
            status_value = "revoked"
            reason_value = ""
        else:
            invalid_client = failure_reason == "revoke_invalid_client"
            invalid_client_streak = prior_invalid_client + 1 if invalid_client else 0
            terminal = invalid_client_streak >= APPLE_REVOCATION_INVALID_CLIENT_TERMINAL
            updates.update(
                consecutive_invalid_client=invalid_client_streak,
                last_error=("terminal:invalid_client" if terminal else f"retry:{failure_reason}"),
                next_attempt_at=(None if terminal else _apple_retry_at(completed_at, attempts)),
            )
            status_value = "terminal" if terminal else "retry"
            reason_value = "invalid_client" if terminal else failure_reason

        updated = AppleRevocationOutbox.objects.filter(
            id=row_id,
            claimed_at=claim_time,
            revoked_at__isnull=True,
        ).update(**updates)
        if not updated:
            results.append({"outbox_id": str(row_id), "status": "stale"})
            continue
        if status_value == "revoked":
            logger.info(
                "auth.apple.revocation.complete outbox_id=%s outcome=%s",
                row_id,
                "apple_4xx" if updates["last_error"] else "revoked",
            )
            results.append(
                {
                    "outbox_id": str(row_id),
                    "status": "revoked",
                    "apple_status": response_status,
                }
            )
        else:
            logger.warning(
                "auth.apple.revocation.%s outbox_id=%s attempt=%s reason=%s",
                "terminal" if status_value == "terminal" else "retry",
                row_id,
                attempts,
                reason_value,
            )
            results.append(
                {
                    "outbox_id": str(row_id),
                    "status": status_value,
                    "reason": reason_value,
                }
            )
    return results


def republish_apple_revocation_outbox_task() -> dict:
    """Zero-argument TASK_MAP wrapper for the existing republish command."""

    from django.core.management import call_command

    call_command("process_apple_revocation_outbox")
    return {"status": "completed"}


# How long past its fire time a one-shot cron is left alone before being retired.
# The container owns firing; this row is bookkeeping. A tenant hibernated across
# the fire time may run the job late on wake, so a generous grace keeps a
# same-hour wake honest without leaving the name squatted for long.
AT_CRON_GRACE = timedelta(hours=1)
INTERNAL_AT_CRON_RETENTION = timedelta(hours=24)


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


def cleanup_internal_crons_task() -> dict:
    """Delete stale, disabled internal one-shot rows after a 24-hour buffer.

    The gateway auto-deletes ``kind:"at"`` jobs after they fire, while the
    hourly retirement sweep above disables the corresponding Postgres rows.
    Internal transients have no user-facing audit value, so retain them for one
    day and then remove them. Recurring, enabled, and user-owned rows are never
    eligible.
    """
    cutoff = timezone.now() - INTERNAL_AT_CRON_RETENTION
    stale_internal = CronJob.objects.filter(
        name__startswith="_",
        enabled=False,
        updated_at__lt=cutoff,
        data__schedule__kind="at",
    ).exclude(source=CronJobSource.USER)
    stale_ids = list(stale_internal.values_list("id", flat=True))
    if not stale_ids:
        logger.info("cleanup_internal_crons: nothing to delete")
        return {"deleted": 0, "ids": []}

    # Reapply the safety predicate for the delete so a concurrent re-enable
    # between the read and write cannot be removed.
    _, deleted_by_model = (
        CronJob.objects.filter(
            id__in=stale_ids,
            name__startswith="_",
            enabled=False,
            updated_at__lt=cutoff,
            data__schedule__kind="at",
        )
        .exclude(source=CronJobSource.USER)
        .delete()
    )
    deleted = deleted_by_model.get("cron.CronJob", 0)
    logger.info("cleanup_internal_crons: deleted %s stale internal at-cron(s) %s", deleted, stale_ids)
    return {"deleted": deleted, "ids": stale_ids}
