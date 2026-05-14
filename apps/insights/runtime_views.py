"""Internal runtime views for the OpenClaw insights plugin.

Mirrors the user-facing endpoints in ``views.py`` but uses internal-runtime
auth (``X-NBHD-Internal-Key`` + ``X-NBHD-Tenant-Id``) instead of JWT. The
plugin in ``runtime/openclaw/plugins/nbhd-insights-tools`` calls these.

Phase 1 — Read tools:

- ``GET runtime/<tenant_id>/history/?pillar=gravity&window=8w&granularity=weekly``
- ``GET runtime/<tenant_id>/snapshots/<uuid>/``
- ``GET runtime/<tenant_id>/compare/?pillar=gravity&period_a=<uuid>&period_b=<uuid>``

Phase 2 — Memory of insights:

- ``GET  runtime/<tenant_id>/baseline/?pillar=gravity&topic=debt&window_weeks=12``
- ``GET  runtime/<tenant_id>/insights/?pillar=gravity&topic=debt&status=open``
- ``POST runtime/<tenant_id>/insights/record/``
- ``POST runtime/<tenant_id>/insights/<uuid>/confirm/``
- ``POST runtime/<tenant_id>/insights/<uuid>/refute/``

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

from .baselines import compute_baseline
from .models import AssistantInsight, PillarSnapshot
from .views import (
    _DIFF_FUNCS,
    ALLOWED_PILLARS,
    DEFAULT_BASELINE_WINDOW_WEEKS,
    DEFAULT_INSIGHT_LIST,
    DEFAULT_WINDOW,
    MAX_BASELINE_WINDOW_WEEKS,
    MAX_INSIGHT_LIST,
    _append_user_response,
    _parse_int,
    _parse_window,
    _record_insight_impl,
    _serialize_insight,
    _serialize_snapshot,
    _validate_record_body,
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


# ── Phase 2: baseline + write-side ────────────────────────────────────────


class RuntimePillarBaselineView(APIView):
    """Internal: rolling baseline stats for (pillar, topic) over a window."""

    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        pillar = (request.query_params.get("pillar") or "").strip().lower()
        if pillar not in ALLOWED_PILLARS:
            return Response(
                {"error": "pillar_not_available", "pillar": pillar, "allowed": sorted(ALLOWED_PILLARS)},
                status=status.HTTP_404_NOT_FOUND,
            )

        topic_slug = (request.query_params.get("topic") or "").strip().lower()
        if not topic_slug:
            return Response(
                {"error": "missing_param", "required": ["topic"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        granularity = (request.query_params.get("granularity") or "weekly").strip().lower()
        if granularity not in dict(PillarSnapshot.Granularity.choices):
            return Response(
                {"error": "invalid_granularity", "granularity": granularity},
                status=status.HTTP_400_BAD_REQUEST,
            )

        window_weeks = _parse_int(
            request.query_params.get("window_weeks"),
            default=DEFAULT_BASELINE_WINDOW_WEEKS,
            lo=1,
            hi=MAX_BASELINE_WINDOW_WEEKS,
        )

        baseline = compute_baseline(
            tenant=tenant,
            pillar=pillar,
            topic_slug=topic_slug,
            window_weeks=window_weeks,
            granularity=granularity,
        )
        return Response(baseline)


class RuntimeInsightListView(APIView):
    """Internal: list AssistantInsight rows for the tenant."""

    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        qs = AssistantInsight.objects.filter(tenant=tenant).select_related("topic")

        pillar = (request.query_params.get("pillar") or "").strip().lower()
        if pillar:
            if pillar not in ALLOWED_PILLARS:
                return Response(
                    {"error": "pillar_not_available", "pillar": pillar, "allowed": sorted(ALLOWED_PILLARS)},
                    status=status.HTTP_404_NOT_FOUND,
                )
            qs = qs.filter(pillar=pillar)

        topic_slug = (request.query_params.get("topic") or "").strip().lower()
        if topic_slug:
            qs = qs.filter(topic__slug=topic_slug)

        status_param = (request.query_params.get("status") or "").strip().lower()
        if status_param:
            valid_statuses = {s.value for s in AssistantInsight.Status}
            if status_param not in valid_statuses:
                return Response(
                    {"error": "invalid_status", "status": status_param},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = qs.filter(status=status_param)

        limit = _parse_int(
            request.query_params.get("limit"),
            default=DEFAULT_INSIGHT_LIST,
            lo=1,
            hi=MAX_INSIGHT_LIST,
        )
        rows = list(qs.order_by("-created_at")[:limit])
        return Response(
            {
                "count": len(rows),
                "insights": [_serialize_insight(r) for r in rows],
            }
        )


class RuntimeRecordInsightView(APIView):
    """Internal: create a new AssistantInsight (status=open)."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        err, cleaned = _validate_record_body(request.data or {})
        if err:
            return err
        ins = _record_insight_impl(tenant=tenant, **cleaned)
        return Response(_serialize_insight(ins), status=status.HTTP_201_CREATED)


class RuntimeConfirmInsightView(APIView):
    """Internal: flip an insight to ``confirmed``."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id, insight_id):
        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        try:
            ins = AssistantInsight.objects.select_related("topic").get(id=insight_id, tenant=tenant)
        except AssistantInsight.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        note = ((request.data or {}).get("note") or "").strip() or None
        ins.status = AssistantInsight.Status.CONFIRMED
        ins.last_confirmed_at = timezone.now()
        _append_user_response(ins, "confirm", note)
        ins.save(update_fields=["status", "last_confirmed_at", "user_responses"])
        return Response(_serialize_insight(ins))


class RuntimeRefuteInsightView(APIView):
    """Internal: flip an insight to ``refuted``."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id, insight_id):
        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        try:
            ins = AssistantInsight.objects.select_related("topic").get(id=insight_id, tenant=tenant)
        except AssistantInsight.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        note = ((request.data or {}).get("note") or "").strip() or None
        ins.status = AssistantInsight.Status.REFUTED
        ins.last_refuted_at = timezone.now()
        _append_user_response(ins, "refute", note)
        ins.save(update_fields=["status", "last_refuted_at", "user_responses"])
        return Response(_serialize_insight(ins))
