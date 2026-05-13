"""Internal runtime views for the OpenClaw insights plugin.

Mirrors the user-facing endpoints in ``views.py`` but uses internal-runtime
auth (``X-NBHD-Internal-Key`` + ``X-NBHD-Tenant-Id``) instead of JWT. The
plugin in ``runtime/openclaw/plugins/nbhd-insights-tools`` calls these.

Routes (under ``/api/v1/insights/``):

- ``GET runtime/<tenant_id>/history/?pillar=gravity&window=8w&granularity=weekly``
- ``GET runtime/<tenant_id>/snapshots/<uuid>/``
- ``GET runtime/<tenant_id>/compare/?pillar=gravity&period_a=<uuid>&period_b=<uuid>``

Phase 1 gating: ``pillar`` must equal ``gravity``; other pillars 404.
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

from .models import PillarSnapshot
from .views import (
    _DIFF_FUNCS,
    ALLOWED_PILLARS,
    DEFAULT_WINDOW,
    _parse_window,
    _serialize_snapshot,
)

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
        return Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist:
        return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)


def _validate_pillar(pillar: str) -> Response | None:
    if pillar not in ALLOWED_PILLARS:
        return Response(
            {"error": "pillar_not_available", "pillar": pillar, "allowed": sorted(ALLOWED_PILLARS)},
            status=status.HTTP_404_NOT_FOUND,
        )
    return None


class RuntimePillarHistoryView(APIView):
    """Internal: list recent snapshots for a pillar within a window."""

    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        pillar = (request.query_params.get("pillar") or "").strip().lower()
        if err := _validate_pillar(pillar):
            return err

        granularity = (request.query_params.get("granularity") or "weekly").strip().lower()
        if granularity not in dict(PillarSnapshot.Granularity.choices):
            return Response(
                {"error": "invalid_granularity", "granularity": granularity},
                status=status.HTTP_400_BAD_REQUEST,
            )

        window = request.query_params.get("window") or DEFAULT_WINDOW
        try:
            window_days = _parse_window(window)
        except ValueError as exc:
            return Response({"error": "invalid_window", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        since = timezone.now() - timezone.timedelta(days=window_days)
        snapshots = list(
            PillarSnapshot.objects.filter(
                tenant=tenant,
                pillar=pillar,
                granularity=granularity,
                ts__gte=since,
            ).order_by("-ts")
        )
        return Response(
            {
                "pillar": pillar,
                "granularity": granularity,
                "window": window,
                "count": len(snapshots),
                "snapshots": [_serialize_snapshot(s) for s in snapshots],
            }
        )


class RuntimePillarSnapshotDetailView(APIView):
    """Internal: return a single snapshot by id (tenant-scoped)."""

    permission_classes = [AllowAny]

    def get(self, request, tenant_id, snapshot_id):
        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        try:
            snap = PillarSnapshot.objects.get(id=snapshot_id, tenant=tenant)
        except PillarSnapshot.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        if err := _validate_pillar(snap.pillar):
            return err

        return Response(_serialize_snapshot(snap))


class RuntimePillarCompareView(APIView):
    """Internal: return both snapshots plus a pillar-specific computed diff."""

    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        pillar = (request.query_params.get("pillar") or "").strip().lower()
        if err := _validate_pillar(pillar):
            return err

        a_id = request.query_params.get("period_a")
        b_id = request.query_params.get("period_b")
        if not a_id or not b_id:
            return Response(
                {"error": "missing_param", "required": ["period_a", "period_b"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        snaps = {str(s.id): s for s in PillarSnapshot.objects.filter(tenant=tenant, pillar=pillar, id__in=[a_id, b_id])}
        if a_id not in snaps or b_id not in snaps:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        a, b = snaps[a_id], snaps[b_id]
        diff_func = _DIFF_FUNCS.get(pillar)
        diff = diff_func(a.payload, b.payload) if diff_func else {}

        return Response(
            {
                "pillar": pillar,
                "period_a": _serialize_snapshot(a),
                "period_b": _serialize_snapshot(b),
                "totals_delta": diff,
            }
        )
