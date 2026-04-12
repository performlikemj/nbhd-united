"""Tenant-facing REST API for cron job management.

Proxies requests to the tenant's OpenClaw Gateway via ``/tools/invoke``.
"""
from __future__ import annotations

import logging

from django.http import Http404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant

from .gateway_client import GatewayError, invoke_gateway_tool

logger = logging.getLogger(__name__)


def _normalize_job_for_universal_isolation(data: dict) -> dict:
    """Force every full cron job dict to isolated/agentTurn before sending to OpenClaw.

    Two-phase cron model: ALL scheduled tasks run isolated. The user-facing
    API still accepts a ``foreground`` boolean (default ``True``) that
    controls whether the Phase 2 sync block is appended to the message body
    by ``_wrap_message_with_phase2`` — this function only handles the
    structural normalization (sessionTarget, payload kind/field).

    Delivery is left untouched: OpenClaw allows ``delivery.channel``/``to``
    on isolated jobs (the previous main-only restriction is gone now that
    every job is isolated), so user-created channel-based delivery still
    works.

    No-op on partial patches: if none of ``sessionTarget``, ``wakeMode``,
    or ``payload`` are in the input, returns the data unchanged. This keeps
    simple `cron.update` patches (e.g. schedule-only edits) flowing through
    the gateway as plain patches.
    """
    triggering_fields = {"sessionTarget", "wakeMode", "payload"}
    if not triggering_fields & set(data.keys()):
        return data

    out = {**data}

    # All cron jobs run isolated
    out["sessionTarget"] = "isolated"
    out.pop("wakeMode", None)  # only valid on main-session jobs

    # Normalize payload to agentTurn/message
    payload = out.get("payload")
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("text", "")
        out["payload"] = {"kind": "agentTurn", "message": message}

    return out


def _strip_phase2_block(message: str) -> str:
    """Remove the Phase 2 sync block from a message if present.

    The block always begins with the wrapper preamble ``\\n\\n---\\n**<MARKER>``,
    so we slice everything before that. Used when toggling a foreground job
    to background — we strip the existing wrapper rather than relying on the
    agent to ignore it.
    """
    from apps.orchestrator.config_generator import PHASE2_SYNC_MARKER

    if not isinstance(message, str) or PHASE2_SYNC_MARKER not in message:
        return message
    sentinel = "\n\n---\n**" + PHASE2_SYNC_MARKER
    head, _, _ = message.partition(sentinel)
    return head


def _wrap_message_with_phase2(message: str, job_name: str, foreground: bool) -> str:
    """Append Phase 2 sync instructions to a job's message if foreground.

    Idempotent: if foreground and the message already contains the Phase 2
    marker, returns it unchanged. If not foreground, strips any existing
    block so toggling off the flag actually removes the wrapper text.
    """
    from apps.orchestrator.config_generator import (
        PHASE2_SYNC_MARKER,
        _phase2_sync_block,
    )

    if not isinstance(message, str):
        return message

    has_marker = PHASE2_SYNC_MARKER in message
    if not foreground:
        return _strip_phase2_block(message) if has_marker else message
    if has_marker:
        return message
    return message + _phase2_sync_block(job_name)


def _message_has_phase2_marker(message: str) -> bool:
    """Detect whether a job's message already contains the Phase 2 sync block."""
    from apps.orchestrator.config_generator import PHASE2_SYNC_MARKER

    return isinstance(message, str) and PHASE2_SYNC_MARKER in message


# System cron jobs that should be hidden from the user's Scheduled Tasks page.
# These are infrastructure tasks — the user gains nothing from seeing them.
HIDDEN_SYSTEM_CRONS = frozenset({
    "Background Tasks",
    "Heartbeat Check-in",
})

# Job-name prefixes that should be hidden from the user-facing UI. Used by
# the two-phase cron pattern: foreground tasks create short-lived
# `_sync:<job name>` crons that fire a summary into the main session and
# then self-remove. We never want users to see them in the UI.
HIDDEN_SYSTEM_CRON_PREFIXES: tuple[str, ...] = ("_sync:",)


def _is_hidden_cron(name: str) -> bool:
    """Whether a cron job name should be hidden from the user's UI."""
    if not name:
        return False
    if name in HIDDEN_SYSTEM_CRONS:
        return True
    return name.startswith(HIDDEN_SYSTEM_CRON_PREFIXES)


def _get_tenant_for_user(user) -> Tenant:
    try:
        return user.tenant
    except Tenant.DoesNotExist as exc:
        raise Http404("Tenant not found.") from exc


def _require_active_tenant(tenant: Tenant) -> None:
    if tenant.status != Tenant.Status.ACTIVE or not tenant.container_fqdn:
        raise GatewayError("Tenant container is not active")


def _tenant_telegram_chat_id(tenant: Tenant) -> int | None:
    return getattr(tenant.user, "telegram_chat_id", None) if tenant.user_id else None


class CronJobListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)
        try:
            _require_active_tenant(tenant)
            result = invoke_gateway_tool(tenant, "cron.list", {})
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        # Snapshot the full job list (including system crons) to PostgreSQL
        # so user-created jobs can be restored after container restarts.
        data = result.get("details", result)
        if isinstance(data, dict) and "jobs" in data:
            try:
                from django.utils import timezone as tz
                # Deduplicate by name — keep newest per name (by createdAt)
                # to prevent dirty snapshots from propagating duplicates.
                raw_jobs = data["jobs"]
                seen: dict[str, dict] = {}
                for job in raw_jobs:
                    if not isinstance(job, dict):
                        continue
                    name = job.get("name", "")
                    if not name:
                        continue
                    prev = seen.get(name)
                    if prev is None or job.get("createdAt", "") > prev.get("createdAt", ""):
                        seen[name] = job
                tenant.cron_jobs_snapshot = {
                    "jobs": list(seen.values()),
                    "snapshot_at": tz.now().isoformat(),
                }
                tenant.save(update_fields=["cron_jobs_snapshot"])
            except Exception:
                logger.warning("Failed to snapshot cron jobs for tenant %s", tenant.id, exc_info=True)

        # Filter out hidden system crons from user-facing list, and enrich
        # each visible job with its `foreground` flag (derived from the
        # presence of the Phase 2 sync marker in the message body — the
        # message itself is the source of truth, no separate storage needed).
        if isinstance(data, dict) and "jobs" in data:
            visible_jobs = []
            for j in data["jobs"]:
                if not isinstance(j, dict):
                    continue
                name = j.get("name", "")
                if _is_hidden_cron(name):
                    continue
                message = ""
                payload = j.get("payload") or {}
                if isinstance(payload, dict):
                    message = payload.get("message") or payload.get("text", "") or ""
                j = {**j, "foreground": _message_has_phase2_marker(message)}
                visible_jobs.append(j)
            data = {**data, "jobs": visible_jobs}
        return Response(data)

    MAX_CRON_JOBS = 10

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        if not data.get("name"):
            return Response(
                {"detail": "Job name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Enforce job cap — check existing job count before creating
        try:
            _require_active_tenant(tenant)
            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        existing_jobs = []
        if isinstance(list_result, dict):
            # Unwrap "details" envelope if present.
            unwrapped = list_result.get("details", list_result)
            existing_jobs = unwrapped.get("jobs", []) if isinstance(unwrapped, dict) else []
        elif isinstance(list_result, list):
            existing_jobs = list_result

        visible_jobs = [
            j for j in existing_jobs
            if isinstance(j, dict) and not _is_hidden_cron(j.get("name", ""))
        ]
        if len(visible_jobs) >= self.MAX_CRON_JOBS:
            return Response(
                {"detail": f"Maximum of {self.MAX_CRON_JOBS} scheduled tasks reached. "
                           "Please delete an existing task before adding a new one."},
                status=status.HTTP_409_CONFLICT,
            )

        # Check for duplicate names
        existing_names = {j.get("name", "").lower() for j in existing_jobs if isinstance(j, dict)}
        new_name = data["name"].strip().lower()
        if new_name in existing_names:
            return Response(
                {"detail": f"A scheduled task named '{data['name'].strip()}' already exists. "
                           "Please use a different name or edit the existing task."},
                status=status.HTTP_409_CONFLICT,
            )

        delivery = data.get("delivery", {})
        if (
            isinstance(delivery, dict)
            and delivery.get("channel") == "telegram"
            and delivery.get("mode") != "none"
        ):
            chat_id = _tenant_telegram_chat_id(tenant)
            if chat_id and not delivery.get("to"):
                data = {**data, "delivery": {**delivery, "to": str(chat_id)}}

        # `foreground` is a Django-only flag — never sent to OpenClaw. We use
        # it to decide whether to append the Phase 2 sync block to the message.
        # Default True per the universal isolation model.
        foreground = bool(data.pop("foreground", True))

        data = _normalize_job_for_universal_isolation(data)

        # Wrap the message after normalization so we know payload.message exists
        payload = data.get("payload") or {}
        if isinstance(payload, dict):
            wrapped = _wrap_message_with_phase2(
                payload.get("message", ""), data["name"], foreground,
            )
            data = {**data, "payload": {**payload, "message": wrapped}}

        try:
            result = invoke_gateway_tool(tenant, "cron.add", {"job": data})
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result), status=status.HTTP_201_CREATED)


class CronJobDetailView(APIView):
    permission_classes = [IsAuthenticated]

    # Fields that the OpenClaw gateway accepts in cron.update patch.
    # "payload" (with message/kind) and arbitrary top-level fields like "id"
    # are rejected — full edits need a delete+recreate cycle. Under the
    # universal isolation model, sessionTarget and wakeMode are no longer
    # user-controlled, so they are not in this set.
    _PATCHABLE_FIELDS = {"schedule", "delivery", "enabled"}

    def patch(self, request, job_name: str):
        if _is_hidden_cron(job_name):
            return Response(
                {"detail": "System tasks cannot be modified."},
                status=status.HTTP_403_FORBIDDEN,
            )
        tenant = _get_tenant_for_user(request.user)
        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        delivery = data.get("delivery")
        if (
            isinstance(delivery, dict)
            and delivery.get("channel") == "telegram"
            and delivery.get("mode") != "none"
        ):
            chat_id = _tenant_telegram_chat_id(tenant)
            if chat_id and not delivery.get("to"):
                data = {**data, "delivery": {**delivery, "to": str(chat_id)}}

        # `foreground` is a Django-only flag — never sent to OpenClaw.
        # If present in the request, we must rebuild the message body and
        # therefore force the delete+recreate path.
        foreground_explicit = "foreground" in data
        foreground_request = bool(data.pop("foreground", True))

        data = _normalize_job_for_universal_isolation(data)

        # If non-patchable fields are present (e.g. payload with message),
        # we must delete+recreate instead of patching in-place. Foreground
        # toggles also require delete+recreate so we can rewrap the message.
        has_unpatchable = bool(set(data.keys()) - self._PATCHABLE_FIELDS)
        if foreground_explicit:
            has_unpatchable = True

        try:
            _require_active_tenant(tenant)

            if has_unpatchable:
                # Fetch existing job to merge with new data
                list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
                # Unwrap "details" envelope — gateway may return
                # {"details": {"jobs": [...]}} or {"jobs": [...]}.
                if isinstance(list_result, dict):
                    list_result = list_result.get("details", list_result)
                jobs = list_result.get("jobs", []) if isinstance(list_result, dict) else list_result
                existing = next((j for j in jobs if j.get("jobId") == job_name or j.get("id") == job_name or j.get("name") == job_name), None)

                # If job not in container, fall back to PostgreSQL snapshot
                # (container restart may have wiped SQLite state).
                job_vanished = False
                if not existing:
                    logger.warning(
                        "cron.update: job '%s' not found in container for tenant %s. "
                        "Available: %s",
                        job_name, tenant.id,
                        [(j.get("jobId"), j.get("name")) for j in jobs],
                    )
                    snapshot = tenant.cron_jobs_snapshot or {}
                    snapshot_jobs = snapshot.get("jobs", [])
                    existing = next(
                        (j for j in snapshot_jobs
                         if j.get("jobId") == job_name or j.get("name") == job_name),
                        None,
                    )
                    if existing:
                        logger.info("cron.update: found job '%s' in snapshot, will recreate", job_name)
                    else:
                        logger.info("cron.update: job '%s' not in snapshot either, creating fresh", job_name)
                        existing = {"name": job_name}
                    job_vanished = True

                # Merge existing job with new data, preserving name and enabled state
                merged = {**existing, **data}
                _STRIP = {"id", "jobId", "createdAt", "state", "createdAtMs", "updatedAtMs", "nextRunAtMs", "runningAtMs"}
                for _f in _STRIP:
                    merged.pop(_f, None)
                merged["name"] = existing.get("name", job_name)
                if "enabled" not in data:
                    merged["enabled"] = existing.get("enabled", True)
                merged = _normalize_job_for_universal_isolation(merged)

                # Decide foreground for the rewritten job:
                # - If the request explicitly set it, honor that.
                # - Otherwise preserve the existing job's foreground state by
                #   detecting the marker in its message.
                if foreground_explicit:
                    foreground_resolved = foreground_request
                else:
                    existing_payload = existing.get("payload") or {}
                    existing_message = ""
                    if isinstance(existing_payload, dict):
                        existing_message = (
                            existing_payload.get("message")
                            or existing_payload.get("text", "")
                            or ""
                        )
                    foreground_resolved = _message_has_phase2_marker(existing_message)

                # Wrap the merged message with Phase 2 (or strip it if going background)
                merged_payload = merged.get("payload") or {}
                if isinstance(merged_payload, dict):
                    base_message = merged_payload.get("message", "")
                    # Strip any existing wrapper before re-wrapping so we never
                    # double-append the block
                    base_message = _strip_phase2_block(base_message)
                    rewrapped = _wrap_message_with_phase2(
                        base_message, merged.get("name", job_name), foreground_resolved,
                    )
                    merged = {
                        **merged,
                        "payload": {**merged_payload, "message": rewrapped},
                    }

                if job_vanished:
                    # Job doesn't exist in container — just create it directly.
                    logger.info("cron.update (create-only) job_name=%s", job_name)
                    result = invoke_gateway_tool(tenant, "cron.add", {"job": merged})
                else:
                    # Use the actual ID from the gateway response for remove
                    gateway_job_id = existing.get("jobId") or existing.get("id") or job_name
                    logger.info("cron.update (delete+create) job_name=%s gateway_id=%s", job_name, gateway_job_id)
                    # Back up existing job before delete so we can rollback if recreate fails
                    backup_job = {k: v for k, v in existing.items() if k not in _STRIP}
                    invoke_gateway_tool(tenant, "cron.remove", {"jobId": gateway_job_id})
                    try:
                        result = invoke_gateway_tool(tenant, "cron.add", {"job": merged})
                    except GatewayError:
                        logger.error("cron.update recreate failed for %s, attempting rollback", job_name)
                        try:
                            invoke_gateway_tool(tenant, "cron.add", {"job": backup_job})
                            logger.info("cron.update rollback succeeded for %s", job_name)
                        except GatewayError:
                            logger.exception("cron.update rollback ALSO failed for %s", job_name)
                        raise
            else:
                logger.info("cron.update job_name=%s patch_keys=%s", job_name, list(data.keys()))
                result = invoke_gateway_tool(
                    tenant, "cron.update", {"jobId": job_name, "patch": data},
                )

            logger.info("cron.update success job_name=%s", job_name)
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result))

    def delete(self, request, job_name: str):
        if _is_hidden_cron(job_name):
            return Response(
                {"detail": "System tasks cannot be deleted."},
                status=status.HTTP_403_FORBIDDEN,
            )
        tenant = _get_tenant_for_user(request.user)
        try:
            _require_active_tenant(tenant)
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_name})
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CronJobToggleView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, job_name: str):
        if _is_hidden_cron(job_name):
            return Response(
                {"detail": "System tasks cannot be modified."},
                status=status.HTTP_403_FORBIDDEN,
            )
        tenant = _get_tenant_for_user(request.user)

        enabled = request.data.get("enabled")
        if enabled is None:
            return Response(
                {"detail": "'enabled' field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            _require_active_tenant(tenant)
            result = invoke_gateway_tool(
                tenant,
                "cron.update",
                {"jobId": job_name, "patch": {"enabled": bool(enabled)}},
            )
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result))


class CronJobBulkDeleteView(APIView):
    """Bulk-delete multiple cron jobs atomically.

    Accepts: POST {"ids": ["name-or-id-1", "name-or-id-2", ...]}
    Returns 200 with per-job results, or 400 if the payload is invalid.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)

        ids = request.data.get("ids")
        if not ids or not isinstance(ids, list):
            return Response(
                {"detail": "'ids' must be a non-empty list of job names/IDs."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_ids: list[str] = []
        for job_id in ids:
            if isinstance(job_id, str) and job_id not in seen:
                seen.add(job_id)
                unique_ids.append(job_id)

        # Block deletion of hidden system crons
        blocked = [jid for jid in unique_ids if _is_hidden_cron(jid)]
        if blocked:
            return Response(
                {"detail": f"System tasks cannot be deleted: {', '.join(blocked)}"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not unique_ids:
            return Response(
                {"detail": "No valid job IDs provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            _require_active_tenant(tenant)
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        results: list[dict] = []
        errors: list[dict] = []

        for job_id in unique_ids:
            try:
                invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
                results.append({"id": job_id, "deleted": True})
                logger.info("cron.bulk_delete: deleted job_id=%s tenant=%s", job_id, tenant.id)
            except GatewayError as exc:
                errors.append({"id": job_id, "deleted": False, "error": str(exc)})
                logger.warning(
                    "cron.bulk_delete: failed to delete job_id=%s tenant=%s error=%s",
                    job_id, tenant.id, exc,
                )

        response_status = status.HTTP_200_OK
        if errors and not results:
            response_status = status.HTTP_502_BAD_GATEWAY
        elif errors:
            response_status = status.HTTP_207_MULTI_STATUS

        return Response(
            {
                "deleted": len(results),
                "errors": len(errors),
                "results": results + errors,
            },
            status=response_status,
        )


class CronJobBulkUpdateForegroundView(APIView):
    """Bulk-update foreground/background mode for multiple cron jobs.

    Accepts: POST {"ids": ["name-or-id-1", ...], "foreground": true|false}
    Returns 200 with per-job results, or 400 if the payload is invalid.
    """

    permission_classes = [IsAuthenticated]

    _STRIP_FIELDS = {"id", "jobId", "createdAt", "state", "createdAtMs", "updatedAtMs", "nextRunAtMs", "runningAtMs"}

    def post(self, request):
        tenant = _get_tenant_for_user(request.user)

        ids = request.data.get("ids")
        if not ids or not isinstance(ids, list):
            return Response(
                {"detail": "'ids' must be a non-empty list of job names/IDs."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        foreground = request.data.get("foreground")
        if foreground is None or not isinstance(foreground, bool):
            return Response(
                {"detail": "'foreground' must be a boolean."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_ids: list[str] = []
        for job_id in ids:
            if isinstance(job_id, str) and job_id not in seen:
                seen.add(job_id)
                unique_ids.append(job_id)

        blocked = [jid for jid in unique_ids if _is_hidden_cron(jid)]
        if blocked:
            return Response(
                {"detail": f"System tasks cannot be modified: {', '.join(blocked)}"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not unique_ids:
            return Response(
                {"detail": "No valid job IDs provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            _require_active_tenant(tenant)
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        # Fetch all jobs from gateway once
        try:
            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
            if isinstance(list_result, dict):
                list_result = list_result.get("details", list_result)
            all_jobs = list_result.get("jobs", []) if isinstance(list_result, dict) else list_result
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        # Build lookup
        job_lookup: dict[str, dict] = {}
        for j in all_jobs:
            for key in (j.get("jobId"), j.get("id"), j.get("name")):
                if key:
                    job_lookup[key] = j

        results: list[dict] = []
        errors: list[dict] = []

        for job_id in unique_ids:
            existing = job_lookup.get(job_id)
            if not existing:
                # Try snapshot fallback
                snapshot = tenant.cron_jobs_snapshot or {}
                snapshot_jobs = snapshot.get("jobs", [])
                existing = next(
                    (j for j in snapshot_jobs if j.get("jobId") == job_id or j.get("name") == job_id),
                    None,
                )
                if not existing:
                    errors.append({"id": job_id, "updated": False, "error": "Job not found"})
                    continue

            # Check if already in desired state
            existing_payload = existing.get("payload") or {}
            existing_message = ""
            if isinstance(existing_payload, dict):
                existing_message = existing_payload.get("message") or existing_payload.get("text", "") or ""
            current_fg = _message_has_phase2_marker(existing_message)
            if current_fg == foreground:
                results.append({"id": job_id, "updated": True, "skipped": True})
                continue

            # Build merged job for recreate
            merged = {k: v for k, v in existing.items() if k not in self._STRIP_FIELDS}
            merged = _normalize_job_for_universal_isolation(merged)

            # Rewrap message
            merged_payload = merged.get("payload") or {}
            if isinstance(merged_payload, dict):
                base_message = _strip_phase2_block(
                    merged_payload.get("message") or merged_payload.get("text", "") or ""
                )
                job_name = merged.get("name") or job_id
                rewrapped = _wrap_message_with_phase2(base_message, job_name, foreground)
                merged = {**merged, "payload": {**merged_payload, "message": rewrapped}}

            gateway_job_id = existing.get("jobId") or existing.get("id") or job_id
            try:
                backup_job = {k: v for k, v in existing.items() if k not in self._STRIP_FIELDS}
                invoke_gateway_tool(tenant, "cron.remove", {"jobId": gateway_job_id})
                try:
                    invoke_gateway_tool(tenant, "cron.add", {"job": merged})
                except GatewayError:
                    logger.error("cron.bulk_foreground: recreate failed for %s, rolling back", job_id)
                    try:
                        invoke_gateway_tool(tenant, "cron.add", {"job": backup_job})
                    except GatewayError:
                        logger.exception("cron.bulk_foreground: rollback ALSO failed for %s", job_id)
                    raise
                results.append({"id": job_id, "updated": True})
                logger.info("cron.bulk_foreground: updated job_id=%s foreground=%s tenant=%s", job_id, foreground, tenant.id)
            except GatewayError as exc:
                errors.append({"id": job_id, "updated": False, "error": str(exc)})
                logger.warning("cron.bulk_foreground: failed job_id=%s tenant=%s error=%s", job_id, tenant.id, exc)

        response_status_code = status.HTTP_200_OK
        if errors and not results:
            response_status_code = status.HTTP_502_BAD_GATEWAY
        elif errors:
            response_status_code = status.HTTP_207_MULTI_STATUS

        return Response(
            {
                "updated": len([r for r in results if r.get("updated")]),
                "errors": len(errors),
                "results": results + errors,
            },
            status=response_status_code,
        )
