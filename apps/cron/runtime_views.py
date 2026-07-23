"""Internal runtime endpoints for the OpenClaw cron module.

Currently a single endpoint: ``RuntimeContainerStartedView`` — called by
the OpenClaw container's startup script after the gateway becomes ready.
Triggers an immediate ``regenerate_tenant_crons`` so a freshly-restarted
SQLite registry is rebuilt from Postgres truth within seconds, without
waiting for the hourly fleet reconcile.

Internal-key auth via ``X-NBHD-Internal-Key`` + ``X-NBHD-Tenant-Id``
headers (mirrors ``apps/fuel/runtime_views.py``).
"""

from __future__ import annotations

import logging
from uuid import UUID

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _internal_auth_or_401(request, tenant_id: UUID) -> Response | None:
    try:
        validate_internal_runtime_request(
            provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
            provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
            expected_tenant_id=str(tenant_id),
        )
    except InternalAuthError as exc:
        return Response(
            {"error": "internal_auth_failed", "detail": str(exc)},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    set_rls_context(tenant_id=tenant_id, service_role=True)
    return None


def _get_tenant_or_404(tenant_id: UUID) -> Tenant | Response:
    try:
        return Tenant.objects.select_related("user").get(id=tenant_id)
    except Tenant.DoesNotExist:
        return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)


class RuntimeContainerStartedView(APIView):
    """POST: signal that an OpenClaw container has finished booting.

    Triggers ``regenerate_tenant_crons`` immediately so the SQLite cron
    registry is rebuilt from Postgres truth without waiting for the hourly
    reconcile. Idempotent — safe to call repeatedly.

    Returns the reconcile summary so the OpenClaw startup script can log
    counts.
    """

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        # Self-heal AGENTS.md on every boot: re-assert the authoritative render
        # (persona + per-tenant gates + Gravity) to the file share. AGENTS.md is
        # seed-once at boot from a provision-time env snapshot that goes stale, so
        # this converges the share to the current render within seconds of ANY
        # restart — including tenants still on the pre-seed-once OpenClaw image.
        # Runs for ALL tenants (before the postgres-canonical gate). Writes the
        # share only (no revision / no restart) and must never fail the hook.
        agents_md_refreshed = False
        try:
            from apps.orchestrator.services import reassert_agents_md

            agents_md_refreshed = reassert_agents_md(tenant)
        except Exception:
            logger.exception("RuntimeContainerStartedView: AGENTS.md re-assert failed for tenant %s", tenant_id)

        # Self-heal SOUL.md / IDENTITY.md the same way: re-assert the managed
        # region (persona baseline) to the share, preserving the agent's growth
        # region below the END marker. Fail-closed per file, never fatal.
        identity_files_refreshed: dict[str, bool] = {}
        try:
            from apps.orchestrator.services import reassert_identity_files

            identity_files_refreshed = reassert_identity_files(tenant)
        except Exception:
            logger.exception("RuntimeContainerStartedView: identity re-assert failed for tenant %s", tenant_id)

        # Self-heal the channel-identity surface on every boot so hibernated
        # tenants converge at wake: TOOLS.md is seed-once with no other reassert
        # path (surgical channel-line swap), and the channel-formatting doc is
        # re-picked by the linkage-based selection (an iOS-only tenant sheds the
        # stale telegram doc). Both are share-only, fail-open, never fatal.
        tools_md_refreshed = False
        tools_md_extras_refreshed = False
        channel_formatting_refreshed = False
        try:
            from apps.orchestrator.services import reassert_tools_md

            tools_md_refreshed = reassert_tools_md(tenant)
        except Exception:
            logger.exception("RuntimeContainerStartedView: TOOLS.md re-assert failed for tenant %s", tenant_id)
        try:
            from apps.orchestrator.services import reassert_tools_md_extras

            tools_md_extras_refreshed = reassert_tools_md_extras(tenant)
        except Exception:
            logger.exception(
                "RuntimeContainerStartedView: TOOLS.md extras re-assert failed for tenant %s",
                tenant_id,
            )
        try:
            from apps.orchestrator.services import reassert_channel_formatting

            channel_formatting_refreshed = reassert_channel_formatting(tenant)
        except Exception:
            logger.exception(
                "RuntimeContainerStartedView: channel-formatting re-assert failed for tenant %s",
                tenant_id,
            )

        if not getattr(tenant, "postgres_cron_canonical", False):
            return Response(
                {
                    "skipped": True,
                    "reason": "tenant not on postgres-canonical flow",
                    "agents_md_refreshed": agents_md_refreshed,
                    "identity_files_refreshed": identity_files_refreshed,
                    "tools_md_refreshed": tools_md_refreshed,
                    "tools_md_extras_refreshed": tools_md_extras_refreshed,
                    "channel_formatting_refreshed": channel_formatting_refreshed,
                },
                status=status.HTTP_200_OK,
            )

        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        try:
            summary = regenerate_tenant_crons(tenant)
        except Exception as exc:
            logger.exception("RuntimeContainerStartedView: regenerate failed for tenant %s", tenant_id)
            return Response(
                {
                    "error": "regenerate_failed",
                    "detail": str(exc),
                    "agents_md_refreshed": agents_md_refreshed,
                    "identity_files_refreshed": identity_files_refreshed,
                    "tools_md_refreshed": tools_md_refreshed,
                    "tools_md_extras_refreshed": tools_md_extras_refreshed,
                    "channel_formatting_refreshed": channel_formatting_refreshed,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            {
                "ok": True,
                "agents_md_refreshed": agents_md_refreshed,
                "identity_files_refreshed": identity_files_refreshed,
                "tools_md_refreshed": tools_md_refreshed,
                "tools_md_extras_refreshed": tools_md_extras_refreshed,
                "channel_formatting_refreshed": channel_formatting_refreshed,
                **summary,
            },
            status=status.HTTP_200_OK,
        )
