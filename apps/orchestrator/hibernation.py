"""Idle hibernation service — scale-to-zero for inactive tenants.

Tenants whose containers have been idle for 2+ hours get their revisions
deactivated (0 replicas, 0 cost). When a message arrives, the container
wakes and buffered messages are auto-forwarded via QStash.

Cron-aware wake: before hibernating, we capture the tenant's cron
schedules and schedule a QStash task to wake the container just before
the next cron fires. After 30 minutes, if no user messages arrived, the
container is re-hibernated (and the next cron wake is scheduled again).

This is distinct from billing-based SUSPENDED status — hibernated tenants
remain status=ACTIVE with a non-null ``hibernated_at`` timestamp.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from django.utils import timezone

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# How early (seconds) to wake the container before a cron fires.
# Container needs ~60s to start + ~60s for crons to resume.
_CRON_WAKE_LEAD_SECONDS = 120

# How long (seconds) to keep a cron-woken container alive before
# re-hibernating if no user messages arrive.
_CRON_WAKE_IDLE_SECONDS = 1800  # 30 minutes


def hibernate_idle_tenant(tenant: Tenant) -> bool:
    """Hibernate a single idle tenant's container.

    Order matters:
    1. Capture cron schedules (container must be reachable)
    2. Suspend crons (container must be reachable)
    3. Deactivate revisions
    4. Schedule next cron wake

    Returns True on success.
    """
    tid = str(tenant.id)[:8]

    # 1. Capture cron schedules before suspending (for cron-aware wake)
    cron_jobs = _capture_tenant_cron_schedules(tenant)

    # 2. Suspend crons while container is still up
    if tenant.container_fqdn:
        try:
            from apps.cron.suspension import suspend_tenant_crons

            result = suspend_tenant_crons(tenant)
            logger.info(
                "idle_hibernate: suspended %d crons for tenant %s",
                result.get("disabled", 0),
                tid,
            )
        except Exception:
            logger.exception(
                "idle_hibernate: failed to suspend crons for %s — proceeding anyway",
                tid,
            )

    # 3. Deactivate all revisions → 0 replicas
    try:
        from apps.orchestrator.azure_client import hibernate_container_app

        hibernate_container_app(tenant.container_id)
    except Exception:
        logger.exception("idle_hibernate: failed to hibernate container for %s", tid)
        return False

    # 4. Mark tenant as hibernated, clear any stale cron_wake_at
    Tenant.objects.filter(id=tenant.id).update(
        hibernated_at=timezone.now(),
        cron_wake_at=None,
    )
    logger.info("idle_hibernate: tenant %s hibernated successfully", tid)

    # 5. Schedule wake for the next cron job
    _schedule_next_cron_wake(tenant, cron_jobs)

    return True


def _capture_tenant_cron_schedules(tenant: Tenant) -> list[dict]:
    """Query tenant's enabled cron jobs and save a snapshot.

    Returns the raw job list for use by ``_schedule_next_cron_wake``.
    """
    if not tenant.container_fqdn:
        return []

    try:
        from apps.cron.gateway_client import invoke_gateway_tool

        result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": False})
        data = result.get("details", result) if isinstance(result, dict) else result
        jobs = data.get("jobs", []) if isinstance(data, dict) else data if isinstance(data, list) else []

        # Persist snapshot for debugging / restore purposes
        Tenant.objects.filter(id=tenant.id).update(
            cron_jobs_snapshot={"jobs": jobs, "snapshot_at": timezone.now().isoformat()},
        )
        return jobs
    except Exception:
        logger.exception(
            "idle_hibernate: failed to capture cron schedules for %s",
            str(tenant.id)[:8],
        )
        return []


def _schedule_next_cron_wake(tenant: Tenant, cron_jobs: list[dict]) -> None:
    """Schedule a QStash task to wake the tenant before their next cron fires."""
    if not cron_jobs:
        return

    now_ms = int(timezone.now().timestamp() * 1000)
    earliest_ms = _find_earliest_next_run(cron_jobs, now_ms)

    if not earliest_ms:
        logger.info(
            "idle_hibernate: no upcoming crons for tenant %s, skipping cron wake",
            str(tenant.id)[:8],
        )
        return

    delay_seconds = max(60, (earliest_ms - now_ms) // 1000 - _CRON_WAKE_LEAD_SECONDS)

    try:
        from apps.cron.publish import publish_task

        publish_task(
            "wake_for_cron",
            str(tenant.id),
            delay_seconds=delay_seconds,
            idempotency_key=f"wake-cron-{tenant.id}-{earliest_ms}",
        )
        logger.info(
            "idle_hibernate: scheduled cron wake for tenant %s in %ds (next cron ~%s)",
            str(tenant.id)[:8],
            delay_seconds,
            datetime.fromtimestamp(earliest_ms / 1000, tz=timezone.utc).isoformat(),
        )
    except Exception:
        logger.exception(
            "idle_hibernate: failed to schedule cron wake for %s",
            str(tenant.id)[:8],
        )


def _find_earliest_next_run(cron_jobs: list[dict], now_ms: int) -> int | None:
    """Return the earliest ``nextRunAtMs`` from the job list.

    Falls back to computing the next run from the cron expression if
    ``nextRunAtMs`` is missing.
    """
    candidates: list[int] = []

    for job in cron_jobs:
        if not job.get("enabled", True):
            continue

        next_run = job.get("nextRunAtMs")
        if next_run and next_run > now_ms:
            candidates.append(next_run)
            continue

        # Fallback: compute from cron expression
        schedule = job.get("schedule", {})
        expr = schedule.get("expr")
        tz_name = schedule.get("tz", "UTC")
        if expr:
            computed = _next_run_from_expr(expr, tz_name)
            if computed and computed > now_ms:
                candidates.append(computed)

    return min(candidates) if candidates else None


def _next_run_from_expr(expr: str, tz_name: str) -> int | None:
    """Compute next run time in epoch ms from a cron expression + timezone."""
    try:
        from zoneinfo import ZoneInfo

        from croniter import croniter

        now = datetime.now(ZoneInfo(tz_name))
        cron = croniter(expr, now)
        next_dt = cron.get_next(datetime)
        return int(next_dt.timestamp() * 1000)
    except Exception:
        logger.debug("Failed to parse cron expr %r tz=%s", expr, tz_name)
        return None


def wake_hibernated_tenant(tenant: Tenant) -> bool:
    """Wake a hibernated tenant's container and schedule follow-up tasks.

    Returns True on success.

    Image refresh on wake: if the latest image tag (``OPENCLAW_IMAGE_TAG``)
    differs from the tenant's stored ``container_image_tag``, push the new
    image instead of activating the existing latest revision. In single-
    revision mode that simultaneously wakes the container AND lands it on
    the current image, fixing the "wake-on-old-image" bug where hibernated
    tenants come back stale because fleet rollouts skip them.
    """
    tid = str(tenant.id)[:8]

    # 1. Wake the container — image-refresh path takes priority over plain wake
    try:
        from django.conf import settings as django_settings

        from apps.orchestrator.azure_client import (
            ensure_plugin_runtime_deps_mount,
            update_container_image,
            wake_container_app,
        )

        desired_tag = getattr(django_settings, "OPENCLAW_IMAGE_TAG", "latest") or "latest"
        current_tag = tenant.container_image_tag or ""
        needs_image_refresh = desired_tag != "latest" and current_tag != desired_tag

        if needs_image_refresh:
            # update_container_image bakes the EmptyDir mount into the same
            # revision as the image bump, so a single restart lands both.
            registry = getattr(django_settings, "AZURE_ACR_SERVER", "nbhdunited.azurecr.io")
            desired_image = f"{registry}/nbhd-openclaw:{desired_tag}"
            update_container_image(tenant.container_id, desired_image)
            Tenant.objects.filter(id=tenant.id).update(container_image_tag=desired_tag)
            logger.info(
                "idle_wake: refreshed image for %s (%s -> %s)",
                tid,
                current_tag[:10] if current_tag else "?",
                desired_tag[:10],
            )
        elif ensure_plugin_runtime_deps_mount(tenant.container_id):
            # In single-revision mode, adding the mount creates a new revision
            # which auto-activates — that wakes the container too. No separate
            # wake call needed.
            logger.info("idle_wake: added plugin-runtime-deps mount and woke %s", tid)
        else:
            wake_container_app(tenant.container_id)
    except Exception:
        logger.exception("idle_wake: failed to wake container for %s", tid)
        return False

    # 2. Clear hibernation flag
    Tenant.objects.filter(id=tenant.id).update(hibernated_at=None)

    # 3. Apply pending config (writes to file share before container finishes booting)
    if tenant.pending_config_version > tenant.config_version:
        try:
            from apps.cron.publish import publish_task

            publish_task("apply_single_tenant_config", str(tenant.id))
            logger.info(
                "idle_wake: queued config apply for %s (v%d→v%d)",
                tid,
                tenant.config_version,
                tenant.pending_config_version,
            )
        except Exception:
            logger.exception("idle_wake: failed to queue config apply for %s", tid)

    # 4. Schedule buffered message delivery (45s delay for container startup)
    try:
        from apps.cron.publish import publish_task

        publish_task(
            "deliver_buffered_messages",
            str(tenant.id),
            delay_seconds=45,
        )
    except Exception:
        logger.exception("idle_wake: failed to schedule buffer delivery for %s", tid)

    # 5. Schedule cron resumption (60s delay — container must be ready)
    try:
        from apps.cron.publish import publish_task

        publish_task(
            "resume_hibernated_crons",
            str(tenant.id),
            delay_seconds=60,
        )
    except Exception:
        logger.exception("idle_wake: failed to schedule cron resume for %s", tid)

    logger.info("idle_wake: tenant %s wake initiated", tid)
    return True


# ---------------------------------------------------------------------------
# Cron-aware wake tasks
# ---------------------------------------------------------------------------


def wake_for_cron_task(tenant_id: str) -> dict:
    """Wake a hibernated tenant's container for a scheduled cron job.

    Called by QStash ~2 minutes before the tenant's next cron is due.
    After 30 minutes, if no user messages arrived, the container is
    re-hibernated via ``check_cron_wake_idle_task``.
    """
    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant:
        logger.warning("wake_for_cron: tenant %s not found", tenant_id[:8])
        return {"status": "tenant_not_found"}

    if not tenant.hibernated_at:
        logger.info("wake_for_cron: tenant %s already awake, skipping", tenant_id[:8])
        return {"status": "already_awake"}

    if tenant.status != Tenant.Status.ACTIVE:
        logger.info(
            "wake_for_cron: tenant %s not active (status=%s), skipping",
            tenant_id[:8],
            tenant.status,
        )
        return {"status": "not_active"}

    # Wake the container (clears hibernated_at, resumes crons, delivers buffers)
    if not wake_hibernated_tenant(tenant):
        return {"status": "wake_failed"}

    # Mark this as a cron-triggered wake
    Tenant.objects.filter(id=tenant.id).update(cron_wake_at=timezone.now())

    # Schedule idle check — if no user messages in 30 min, re-hibernate
    try:
        from apps.cron.publish import publish_task

        publish_task(
            "check_cron_wake_idle",
            str(tenant.id),
            delay_seconds=_CRON_WAKE_IDLE_SECONDS,
        )
    except Exception:
        logger.exception(
            "wake_for_cron: failed to schedule idle check for %s",
            tenant_id[:8],
        )

    logger.info("wake_for_cron: tenant %s woken for scheduled cron", tenant_id[:8])
    return {"status": "woken_for_cron"}


def check_cron_wake_idle_task(tenant_id: str) -> dict:
    """Check if a cron-woken tenant should be re-hibernated.

    Called 30 minutes after a cron wake. If no user messages were sent
    during the wake window, hibernate immediately (which also schedules
    the next cron wake). If the user messaged, clear ``cron_wake_at``
    and let normal idle detection handle it.
    """
    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant:
        return {"status": "tenant_not_found"}

    if not tenant.cron_wake_at:
        logger.info(
            "check_cron_wake_idle: tenant %s has no cron_wake_at, skipping",
            tenant_id[:8],
        )
        return {"status": "not_cron_wake"}

    if tenant.hibernated_at:
        logger.info(
            "check_cron_wake_idle: tenant %s already hibernated, skipping",
            tenant_id[:8],
        )
        return {"status": "already_hibernated"}

    # Did the user send any messages since the cron wake?
    user_messaged = tenant.last_message_at and tenant.last_message_at > tenant.cron_wake_at

    if user_messaged:
        # User is active — hand off to normal idle detection (2h threshold)
        Tenant.objects.filter(id=tenant.id).update(cron_wake_at=None)
        logger.info(
            "check_cron_wake_idle: tenant %s has user activity, staying awake",
            tenant_id[:8],
        )
        return {"status": "user_active"}

    # No user activity — re-hibernate (this also schedules the next cron wake)
    logger.info(
        "check_cron_wake_idle: tenant %s idle after cron wake, re-hibernating",
        tenant_id[:8],
    )
    Tenant.objects.filter(id=tenant.id).update(cron_wake_at=None)
    tenant.refresh_from_db()
    hibernate_idle_tenant(tenant)

    return {"status": "re_hibernated"}


_MAX_DELIVERY_ATTEMPTS = 3
_TRANSIENT_BACKOFFS_SECONDS: tuple[float, ...] = (5.0, 15.0, 45.0)


def _post_chat_completion_with_backoff(
    url: str,
    *,
    payload: dict,
    headers: dict,
    timeout: float = 120.0,
    backoffs: tuple[float, ...] = _TRANSIENT_BACKOFFS_SECONDS,
):
    """POST to OpenClaw `/v1/chat/completions` with retry on transient errors.

    A "transient" error is a connection/timeout error or any 5xx response —
    typically a still-cold container or a brief gateway hiccup. Those get
    retried with the given backoffs *before* this counts as a failed
    delivery attempt against the buffered message. Permanent errors (4xx)
    raise immediately.

    Returns the parsed JSON body on success.
    """

    import httpx

    delays = [0.0, *backoffs]
    last_error: Exception | None = None
    for i, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning(
                "post_chat: transient %s on attempt %d/%d",
                type(exc).__name__,
                i + 1,
                len(delays),
            )
            continue

        if resp.status_code >= 500:
            last_error = httpx.HTTPStatusError(
                f"Server error {resp.status_code}",
                request=resp.request,
                response=resp,
            )
            logger.warning(
                "post_chat: %d on attempt %d/%d",
                resp.status_code,
                i + 1,
                len(delays),
            )
            continue

        resp.raise_for_status()
        return resp.json()

    assert last_error is not None
    raise last_error


def _send_apology_for_dropped_message(tenant: Tenant, msg) -> None:
    """Notify the user we couldn't process their buffered message after the
    attempts cap. Uses channel-native plain push (NOT
    `relay_ai_response_to_line`) since this is system status, not assistant
    content. Localized via the existing `error_msg` framework — falls back
    to English for languages without a translated key."""
    from apps.router.error_messages import error_msg
    from apps.router.models import BufferedMessage

    excerpt = (msg.user_text or "").strip().replace("\n", " ")
    if len(excerpt) > 50:
        excerpt = excerpt[:50] + "\u2026"

    lang = getattr(tenant.user, "language", None) or "en"
    if excerpt:
        text = error_msg(lang, "dropped_message_with_excerpt", excerpt=excerpt)
    else:
        text = error_msg(lang, "dropped_message")

    if msg.channel == BufferedMessage.Channel.LINE:
        line_user_id = getattr(tenant.user, "line_user_id", None)
        if not line_user_id:
            return
        from apps.router.line_webhook import _send_line_text

        try:
            _send_line_text(line_user_id, text)
        except Exception:
            logger.exception(
                "deliver_buffered: failed to push apology to LINE for tenant %s",
                str(tenant.id)[:8],
            )
    else:
        # Telegram apology not yet wired up — Telegram path uses
        # forward_to_openclaw which has its own retry envelope, so the
        # head-of-line stall pattern is less acute here. Log only.
        logger.info(
            "deliver_buffered: dropped Telegram msg for tenant %s (apology not impl)",
            str(tenant.id)[:8],
        )


def deliver_buffered_messages_task(tenant_id: str) -> dict:
    """Forward all buffered messages for a tenant to its container.

    Called via QStash ~45s after wake to give the container time to start.

    Resilience semantics (regression guard for 2026-04-28 head-of-line
    incident):
      - Transient 5xx / connection errors retry inside the task with
        backoff before counting as a delivery attempt.
      - On a real per-message failure we increment ``delivery_attempts``
        and break to preserve queue order; QStash retries the task.
      - Once a message has hit ``_MAX_DELIVERY_ATTEMPTS`` we mark it
        ``delivered=True / status=failed`` and push a one-shot apology
        to the user so the head of the queue can never block forever.
    """
    import asyncio

    from django.conf import settings

    from apps.router.models import BufferedMessage
    from apps.router.services import forward_to_openclaw

    tenant = Tenant.objects.select_related("user").filter(id=tenant_id).first()
    if not tenant or not tenant.container_fqdn:
        logger.warning("deliver_buffered: tenant %s not found or no FQDN", tenant_id[:8])
        return {"delivered": 0, "failed": 0, "dropped": 0}

    messages = BufferedMessage.objects.filter(
        tenant=tenant,
        delivered=False,
    ).order_by("created_at")

    delivered = 0
    failed = 0
    dropped = 0

    for msg in messages:
        # Drop messages past the attempts cap so they don't block the
        # queue forever, then notify the user.
        if msg.delivery_attempts >= _MAX_DELIVERY_ATTEMPTS:
            logger.warning(
                "deliver_buffered: dropping msg %s for tenant %s after %d attempts",
                msg.id,
                tenant_id[:8],
                msg.delivery_attempts,
            )
            msg.delivered = True
            msg.delivered_at = timezone.now()
            msg.delivery_status = BufferedMessage.Status.FAILED
            msg.save(update_fields=["delivered", "delivered_at", "delivery_status"])
            _send_apology_for_dropped_message(tenant, msg)
            dropped += 1
            continue

        try:
            if msg.channel == BufferedMessage.Channel.TELEGRAM:
                loop = asyncio.new_event_loop()
                try:
                    user_tz = tenant.user.timezone or "UTC"
                    loop.run_until_complete(
                        forward_to_openclaw(
                            tenant.container_fqdn,
                            msg.payload,
                            user_timezone=user_tz,
                            timeout=30.0,
                            max_retries=1,
                            retry_delay=5.0,
                        )
                    )
                finally:
                    loop.close()

            elif msg.channel == BufferedMessage.Channel.LINE:
                url = f"https://{tenant.container_fqdn}/v1/chat/completions"
                gateway_token = getattr(settings, "NBHD_INTERNAL_API_KEY", "").strip()
                user_tz = tenant.user.timezone or "UTC"
                line_user_id = tenant.user.line_user_id or ""

                result = _post_chat_completion_with_backoff(
                    url,
                    payload={
                        "model": "openclaw",
                        "messages": [{"role": "user", "content": msg.user_text or "..."}],
                        "user": line_user_id,
                    },
                    headers={
                        "Authorization": f"Bearer {gateway_token}",
                        "X-User-Timezone": user_tz,
                        "X-Line-User-Id": line_user_id,
                    },
                )

                # Send response back via LINE — use the same pipeline as the
                # live webhook so markdown stripping, Flex bubbles, charts,
                # and PII rehydration all apply (no reply_token: buffered
                # delivery happens long after the webhook reply window).
                ai_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                if ai_text and line_user_id:
                    from apps.router.line_webhook import relay_ai_response_to_line

                    relay_ai_response_to_line(tenant, line_user_id, ai_text)

            msg.delivered = True
            msg.delivered_at = timezone.now()
            msg.delivery_status = BufferedMessage.Status.DELIVERED
            msg.save(update_fields=["delivered", "delivered_at", "delivery_status"])
            delivered += 1

        except Exception:
            logger.exception(
                "deliver_buffered: failed to deliver msg %s for tenant %s (attempt %d/%d)",
                msg.id,
                tenant_id[:8],
                msg.delivery_attempts + 1,
                _MAX_DELIVERY_ATTEMPTS,
            )
            msg.delivery_attempts += 1
            msg.save(update_fields=["delivery_attempts"])
            failed += 1
            # Stop processing further messages to preserve order.
            # QStash will retry the task; on the next cycle this message
            # will be retried (and eventually dropped if it keeps failing).
            break

    logger.info(
        "deliver_buffered: tenant %s — delivered=%d failed=%d dropped=%d",
        tenant_id[:8],
        delivered,
        failed,
        dropped,
    )

    if failed > 0:
        # Surface a non-2xx so QStash retries the task. Dropped messages
        # don't count — they've already been resolved (apology sent).
        raise RuntimeError(f"deliver_buffered: {failed} message(s) failed for tenant {tenant_id[:8]}")

    return {"delivered": delivered, "failed": failed, "dropped": dropped}


def resume_hibernated_crons_task(tenant_id: str) -> None:
    """Resume crons for a freshly-woken tenant. Called via QStash ~60s after wake."""
    from apps.cron.suspension import resume_tenant_crons

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant:
        return

    try:
        result = resume_tenant_crons(tenant)
        logger.info(
            "resume_hibernated_crons: tenant %s — enabled=%d",
            tenant_id[:8],
            result.get("enabled", 0),
        )
    except Exception:
        logger.exception("resume_hibernated_crons: failed for tenant %s", tenant_id[:8])
        raise


def cleanup_delivered_buffers_task() -> dict:
    """Delete delivered BufferedMessage rows older than 7 days."""
    from datetime import timedelta

    from apps.router.models import BufferedMessage

    cutoff = timezone.now() - timedelta(days=7)
    deleted, _ = BufferedMessage.objects.filter(
        delivered=True,
        created_at__lt=cutoff,
    ).delete()

    logger.info("cleanup_delivered_buffers: deleted %d old messages", deleted)
    return {"deleted": deleted}
