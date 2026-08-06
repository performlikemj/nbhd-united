"""Background tasks for the router app."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from django.conf import settings

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

MEDIA_DIR = "workspace/media/inbound"
MAX_AGE = timedelta(hours=24)

# Wall-clock ceiling for the extraction delivery turn. The agent is answering the
# user's real question against the document, so this is a full turn, not a ping —
# matches ``broadcast_single_tenant_task``'s budget for the same kind of
# Django-initiated turn.
_EXTRACTION_TURN_TIMEOUT = 120.0

# ``job_name`` stamped on the ProactiveOutbound row this task's delivery writes.
# Leading underscore marks it platform-internal, matching
# ``FIRST_SESSION_WELCOME_JOB_NAME``.
EXTRACTION_JOB_NAME = "_document_extraction"


def cleanup_inbound_media_task() -> None:
    """Delete inbound media files older than 24 hours from all tenant file shares.

    Called via QStash cron schedule (daily).
    """
    from azure.storage.fileshare import ShareDirectoryClient

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        logger.warning("AZURE_STORAGE_ACCOUNT_NAME not configured, skipping media cleanup")
        return

    from apps.orchestrator.azure_client import _is_mock, get_storage_client

    if _is_mock():
        logger.info("[MOCK] Would clean up inbound media for all tenants")
        return

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account_name,
    )
    account_key = keys.keys[0].value

    cutoff = datetime.now(UTC) - MAX_AGE
    total_deleted = 0

    tenants = Tenant.objects.filter(status=Tenant.Status.ACTIVE).exclude(container_id="")
    for tenant in tenants:
        share_name = f"ws-{str(tenant.id)[:20]}"
        try:
            dir_client = ShareDirectoryClient(
                account_url=f"https://{account_name}.file.core.windows.net",
                share_name=share_name,
                directory_path=MEDIA_DIR,
                credential=account_key,
            )
            files = list(dir_client.list_directories_and_files())
        except Exception:
            # Directory doesn't exist yet — no media uploaded for this tenant
            continue

        for item in files:
            if item.get("is_directory"):
                continue
            # Check last modified time
            try:
                file_props = dir_client.get_file_client(item["name"]).get_file_properties()
                last_modified = file_props.last_modified
                if last_modified and last_modified < cutoff:
                    dir_client.get_file_client(item["name"]).delete_file()
                    total_deleted += 1
            except Exception:
                logger.debug("Failed to check/delete %s in %s", item["name"], share_name)

    logger.info("Media cleanup complete: deleted %d files across %d tenants", total_deleted, tenants.count())


def _deliver_extraction_turn(tenant: Tenant, thread_id: str, turn_text: str) -> None:
    """Hand one extraction result to the tenant's agent, then to the user.

    Two legs, both mirroring shipped code:

    1. **The agent turn.** POST to the container's ``/v1/chat/completions`` with
       the gateway bearer resolved by ``apps.cron.gateway_client``, exactly like
       ``orchestrator.tasks.broadcast_single_tenant_task`` and the iOS drain
       (``pending_queue._drain_ios_batch``). ``user`` is the SAME
       ``thread:<id>`` session param the user's original message carried — that
       is what puts this turn in the session holding the request the agent is
       being told to answer. Send it to a different session and the agent gets a
       document with no question attached.

    2. **The user delivery.** The agent's reply comes back in the response body
       (there is no ``AppChatMessage`` row for a Django-initiated turn, so the
       drain's persistence path does not apply here). It reaches the user through
       ``record_proactive_outbound`` — the same call
       ``orchestrator.first_session_welcome.seed_first_session_welcome`` makes —
       which lands the message in the ``?since=`` feed and fires the APNs push.

    Raises on transport/HTTP failure so QStash redelivers; the caller only writes
    the done-marker after this returns cleanly.
    """
    # Invariant 7: tenant timezone through the front door, never a private helper.
    from apps.common.tenant_tz import tenant_tz_name
    from apps.cron.gateway_client import get_gateway_token_for_tenant
    from apps.router.pending_queue import _extract_ai_response
    from apps.router.proactive_context import record_proactive_outbound

    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    payload = {
        "model": "openclaw",
        "messages": [{"role": "user", "content": turn_text}],
    }
    if thread_id:
        payload["user"] = f"thread:{thread_id}"

    resp = httpx.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {get_gateway_token_for_tenant(tenant)}",
            "X-User-Timezone": tenant_tz_name(tenant),
            "X-Channel": "ios",
        },
        timeout=_EXTRACTION_TURN_TIMEOUT,
    )
    resp.raise_for_status()

    reply = _extract_ai_response(resp.json())
    if not reply:
        # The turn ran but produced nothing usable. Don't invent a message for
        # the user — log it and let the done-marker be written anyway, because
        # re-running the identical turn would produce the identical nothing.
        logger.warning(
            "pdf_extract: delivery turn for tenant %s returned no assistant text",
            str(tenant.id)[:8],
        )
        return

    record_proactive_outbound(
        tenant=tenant,
        channel="app",
        channel_user_id=str(tenant.user_id),
        message_text=reply,
        job_name=EXTRACTION_JOB_NAME,
    )


def extract_inbound_document_task(tenant_id: str, workspace_path: str, thread_id: str = "") -> dict:
    """Extract an app-uploaded PDF's text off the agent's turn, then deliver it.

    Enqueued by the iOS chat ingress (``apps.router.chat_views.enqueue_tenant_turn``)
    right after ``store_inbound_document`` writes the file to the tenant share.
    See ``apps.router.pdf_extraction`` for the phase scope and the on-share state
    model.

    **Idempotency is file presence, not a DB row** (the design forbids new
    tables). The done-marker (``<doc>.delivered``) is checked before any delivery
    and written only after one succeeds, so a QStash redelivery of an
    already-delivered extraction is a clean no-op rather than a second
    unexplained message. The ordering is deliberate: marker AFTER delivery means
    a crash between the two re-delivers once (mildly annoying), whereas marker
    BEFORE delivery would mean a failed POST leaves the user waiting forever on
    a follow-up that can never be retried. Silent hang is the bigger wrong.

    Re-extraction itself is naturally idempotent — the stored filename is
    content-addressed, so the same document always yields the same text at the
    same path.

    Returns a small status dict for the QStash trigger log; never returns None.
    """
    from apps.orchestrator.azure_client import (
        download_workspace_file,
        download_workspace_file_binary,
        upload_workspace_file,
    )
    from apps.router.pdf_extraction import (
        build_extraction_fallback_turn,
        build_extraction_ready_turn,
        delivery_marker_path,
        extract_pdf_text,
        extracted_text_path,
        redact_extracted_document_text,
    )

    tenant = Tenant.objects.filter(id=tenant_id).select_related("user").first()
    if not tenant or not tenant.container_fqdn:
        logger.info("pdf_extract: tenant %s missing or has no container — skipping", str(tenant_id)[:8])
        return {"status": "no_tenant"}
    if not tenant.has_entitlement or tenant.status != Tenant.Status.ACTIVE:
        logger.info("pdf_extract: tenant %s not active/entitled — skipping", str(tenant_id)[:8])
        return {"status": "not_active"}

    marker_path = delivery_marker_path(workspace_path)
    if download_workspace_file(str(tenant.id), marker_path) is not None:
        logger.info("pdf_extract: tenant %s already delivered %s — no-op", str(tenant_id)[:8], marker_path)
        return {"status": "already_delivered"}

    # Decide what to tell the agent. Every branch produces a turn — a document
    # the async path can't handle is handed back to the in-turn ``pdf`` tool
    # rather than dropped, so the user is never left waiting silently.
    char_count = 0
    try:
        data = download_workspace_file_binary(str(tenant.id), workspace_path)
        if not data:
            outcome = "missing"
            turn_text = build_extraction_fallback_turn(
                workspace_path=workspace_path, reason="the stored file could not be read back"
            )
        else:
            text = redact_extracted_document_text(tenant, extract_pdf_text(data))
            if text:
                # Invariant 2: text writes go through the sanitize chokepoint.
                upload_workspace_file(str(tenant.id), extracted_text_path(workspace_path), text)
                char_count = len(text)
                outcome = "extracted"
                turn_text = build_extraction_ready_turn(workspace_path=workspace_path, text=text)
            else:
                # Phase 1 does not OCR. The in-turn Gemma vision path still works.
                outcome = "no_text_layer"
                turn_text = build_extraction_fallback_turn(
                    workspace_path=workspace_path,
                    reason="it is a scanned or image-only PDF with no text layer",
                )
    except Exception:
        logger.exception("pdf_extract: extraction failed for tenant %s", str(tenant_id)[:8])
        outcome = "failed"
        turn_text = build_extraction_fallback_turn(
            workspace_path=workspace_path, reason="server-side extraction hit an error"
        )

    # Delivery failures propagate so QStash retries the whole (idempotent) task.
    _deliver_extraction_turn(tenant, thread_id, turn_text)

    # Best-effort: delivery already happened, so a marker-write failure must not
    # fail the task. The cost of losing it is one duplicate follow-up on a retry.
    try:
        upload_workspace_file(str(tenant.id), marker_path, f"{outcome} {datetime.now(UTC).isoformat()}\n")
    except Exception:
        logger.warning(
            "pdf_extract: done-marker write failed for tenant %s (delivery already sent)",
            str(tenant_id)[:8],
            exc_info=True,
        )

    # Telemetry only — never the filename or any extracted content (the
    # no-raw-value discipline ``_store_inbound_media`` follows).
    logger.info("pdf_extract_done tenant=%s outcome=%s chars=%d", tenant_id, outcome, char_count)
    return {"status": outcome, "chars": char_count}


def poll_line_quota_task() -> dict:
    """Daily poll of the LINE Push monthly quota.

    Refreshes :class:`apps.router.models.LineQuotaState` from the LINE
    Messaging API and, on any threshold crossing, enqueues
    ``dispatch_line_quota_handler`` so the fan-out (emails + channel
    flips) happens out-of-band. The handler is idempotent, so it's
    fine for both this task and the 429 tripwire to enqueue it for
    the same event.

    Cadence: once daily (registered via ``register_system_crons``).
    """
    from apps.cron.publish import publish_task
    from apps.router.line_quota import refresh_quota_state

    result = refresh_quota_state()

    if result.transitions:
        try:
            publish_task("dispatch_line_quota_handler")
        except Exception:
            logger.exception("poll_line_quota: failed to enqueue handler dispatch")

    return {
        "polled": result.polled,
        "limit": result.limit,
        "used": result.used,
        "transitions": list(result.transitions),
    }


def dispatch_line_quota_handler_task() -> dict:
    """Run the LINE quota state-transition handlers (pre-warn email,
    exhaustion fan-out, recovery fan-out). Idempotent — each handler
    short-circuits if its event has already been notified.

    Enqueued by:
      - ``poll_line_quota_task`` when the daily poll detects a transition
      - The 429 tripwire in ``apps.router.line_webhook._maybe_trip_monthly_quota``
        immediately on exhaustion (so users don't wait up to 24h for
        the email after the cap is hit mid-day).
    """
    from apps.router.line_quota_handlers import dispatch_for_current_state

    return dispatch_for_current_state()
