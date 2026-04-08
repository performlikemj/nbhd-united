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


def _fix_payload_kind_for_session_target(data: dict) -> dict:
    """Fix payload kind AND field name for the session target.

    OpenClaw's cron schema uses ``anyOf`` validation:
    - ``systemEvent`` requires ``{kind: "systemEvent", text: "..."}``
    - ``agentTurn``   requires ``{kind: "agentTurn", message: "..."}``

    The frontend always sends ``agentTurn`` + ``message``, so we correct
    both the kind and the content field name here.
    """
    session_target = data.get("sessionTarget", "main")
    payload = data.get("payload")
    if isinstance(payload, dict) and session_target == "main":
        text = payload.get("text") or payload.get("message", "")
        data = {**data, "payload": {"kind": "systemEvent", "text": text}}
    elif isinstance(payload, dict) and session_target != "main":
        message = payload.get("message") or payload.get("text", "")
        data = {**data, "payload": {"kind": "agentTurn", "message": message}}
    return data


# System cron jobs that should be hidden from the user's Scheduled Tasks page.
# These are infrastructure tasks — the user gains nothing from seeing them.
HIDDEN_SYSTEM_CRONS = frozenset({
    "Background Tasks",
    "Heartbeat Check-in",
})


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

        # Filter out hidden system crons from user-facing list
        if isinstance(data, dict) and "jobs" in data:
            data = {
                **data,
                "jobs": [
                    j for j in data["jobs"]
                    if j.get("name") not in HIDDEN_SYSTEM_CRONS
                ],
            }
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

        if len(existing_jobs) >= self.MAX_CRON_JOBS:
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

        data = _fix_payload_kind_for_session_target(data)

        try:
            result = invoke_gateway_tool(tenant, "cron.add", {"job": data})
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result.get("details", result), status=status.HTTP_201_CREATED)


class CronJobDetailView(APIView):
    permission_classes = [IsAuthenticated]

    # Fields that the OpenClaw gateway accepts in cron.update patch.
    # "payload" (with message/kind) and arbitrary top-level fields like "id"
    # are rejected — full edits need a delete+recreate cycle.
    _PATCHABLE_FIELDS = {"schedule", "sessionTarget", "wakeMode", "delivery", "enabled"}

    def patch(self, request, job_name: str):
        if job_name in HIDDEN_SYSTEM_CRONS:
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

        data = _fix_payload_kind_for_session_target(data)

        # If non-patchable fields are present (e.g. payload with message),
        # we must delete+recreate instead of patching in-place.
        # Also force delete+recreate when sessionTarget is changing —
        # OpenClaw requires the existing payload to be re-shaped
        # (kind/text vs kind/message) to match the new session target,
        # which the simple-patch path can't do.
        has_unpatchable = bool(set(data.keys()) - self._PATCHABLE_FIELDS)
        if "sessionTarget" in data:
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
                merged = _fix_payload_kind_for_session_target(merged)

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
        if job_name in HIDDEN_SYSTEM_CRONS:
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
        if job_name in HIDDEN_SYSTEM_CRONS:
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
        blocked = [jid for jid in unique_ids if jid in HIDDEN_SYSTEM_CRONS]
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
