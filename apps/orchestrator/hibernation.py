"""Idle hibernation service — scale-to-zero for inactive tenants.

Tenants whose containers have been idle for 24+ hours get their revisions
deactivated (0 replicas, 0 cost). When a message arrives, the container
wakes and buffered messages are auto-forwarded via QStash.

This is distinct from billing-based SUSPENDED status — hibernated tenants
remain status=ACTIVE with a non-null ``hibernated_at`` timestamp.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def hibernate_idle_tenant(tenant: Tenant) -> bool:
    """Hibernate a single idle tenant's container.

    Order matters: suspend crons first (container must be reachable),
    then deactivate revisions.

    Returns True on success.
    """
    tid = str(tenant.id)[:8]

    # 1. Suspend crons while container is still up
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

    # 2. Deactivate all revisions → 0 replicas
    try:
        from apps.orchestrator.azure_client import hibernate_container_app

        hibernate_container_app(tenant.container_id)
    except Exception:
        logger.exception("idle_hibernate: failed to hibernate container for %s", tid)
        return False

    # 3. Mark tenant as hibernated
    Tenant.objects.filter(id=tenant.id).update(hibernated_at=timezone.now())
    logger.info("idle_hibernate: tenant %s hibernated successfully", tid)
    return True


def wake_hibernated_tenant(tenant: Tenant) -> bool:
    """Wake a hibernated tenant's container and schedule follow-up tasks.

    Does NOT clear hibernated_at here — that happens in
    deliver_buffered_messages_task() after successful delivery, proving
    the container is actually healthy.  This prevents the deadlock where
    a failed wake leaves the tenant permanently stuck.

    Returns True on success.
    """
    tid = str(tenant.id)[:8]

    # 1. Activate latest revision (best-effort — delivery task retries if this fails)
    try:
        from apps.orchestrator.azure_client import wake_container_app

        wake_container_app(tenant.container_id)
    except Exception:
        logger.exception("idle_wake: failed to wake container for %s", tid)

    # 2. Schedule buffered message delivery (45s delay for container startup)
    #    On success, the delivery task clears hibernated_at and resumes crons.
    try:
        from apps.cron.publish import publish_task

        publish_task(
            "deliver_buffered_messages",
            str(tenant.id),
            delay_seconds=45,
        )
    except Exception:
        logger.exception("idle_wake: failed to schedule buffer delivery for %s", tid)

    logger.info("idle_wake: tenant %s wake initiated", tid)
    return True


def deliver_buffered_messages_task(tenant_id: str) -> dict:
    """Forward all buffered messages for a tenant to its container.

    Called via QStash ~45s after wake to give the container time to start.
    """
    import asyncio

    import httpx
    from django.conf import settings

    from apps.router.models import BufferedMessage
    from apps.router.services import forward_to_openclaw

    tenant = Tenant.objects.select_related("user").filter(id=tenant_id).first()
    if not tenant or not tenant.container_fqdn:
        logger.warning("deliver_buffered: tenant %s not found or no FQDN", tenant_id[:8])
        return {"delivered": 0, "failed": 0}

    messages = BufferedMessage.objects.filter(
        tenant=tenant, delivered=False,
    ).order_by("created_at")

    delivered = 0
    failed = 0

    for msg in messages:
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

                resp = httpx.post(
                    url,
                    json={
                        "model": "openclaw",
                        "messages": [{"role": "user", "content": msg.payload.get("message", {}).get("text", "") or msg.user_text or "..."}],
                        "user": line_user_id,
                    },
                    headers={
                        "Authorization": f"Bearer {gateway_token}",
                        "X-User-Timezone": user_tz,
                        "X-Line-User-Id": line_user_id,
                    },
                    timeout=120.0,
                )
                resp.raise_for_status()

                # Send response back via LINE
                result = resp.json()
                ai_text = (
                    result.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                if ai_text and line_user_id:
                    from apps.router.line_webhook import _send_line_text

                    _send_line_text(line_user_id, ai_text)

            msg.delivered = True
            msg.delivered_at = timezone.now()
            msg.save(update_fields=["delivered", "delivered_at"])
            delivered += 1

        except Exception:
            logger.exception(
                "deliver_buffered: failed to deliver msg %s for tenant %s",
                msg.id, tenant_id[:8],
            )
            failed += 1
            raise  # Let QStash retry

    logger.info(
        "deliver_buffered: tenant %s — delivered=%d failed=%d",
        tenant_id[:8], delivered, failed,
    )

    # Only exit hibernation once ALL messages are delivered successfully,
    # proving the container is healthy.
    if delivered > 0 and failed == 0:
        Tenant.objects.filter(id=tenant.id).update(hibernated_at=None)
        logger.info("deliver_buffered: tenant %s — hibernation cleared", tenant_id[:8])

        # Schedule cron resumption (15s delay — container already warm)
        try:
            from apps.cron.publish import publish_task

            publish_task(
                "resume_hibernated_crons",
                str(tenant.id),
                delay_seconds=15,
            )
        except Exception:
            logger.exception(
                "deliver_buffered: failed to schedule cron resume for %s", tenant_id[:8]
            )

    return {"delivered": delivered, "failed": failed}


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
            tenant_id[:8], result.get("enabled", 0),
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
