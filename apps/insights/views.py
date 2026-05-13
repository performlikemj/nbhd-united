"""User-facing API for the assistant baseline / insights subsystem.

Day-1 endpoints surface pillar snapshots to the authenticated tenant user:

- ``GET /api/v1/insights/history/?pillar=gravity&window=8w&granularity=weekly``
- ``GET /api/v1/insights/snapshots/<uuid>/``
- ``GET /api/v1/insights/compare/?pillar=gravity&period_a=<uuid>&period_b=<uuid>``

OpenClaw-runtime-compatible mirrors (X-NBHD-Internal-Key auth) live in
``runtime_views.py`` and ship in Day 2.

Pillar gating: Phase 1 only allows ``pillar=gravity``. Other pillars are
404'd until their snapshot pipelines ship.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PillarSnapshot
from .pillars import Pillar

ALLOWED_PILLARS = {Pillar.GRAVITY.value}
DEFAULT_WINDOW = "12w"
MAX_WINDOW_DAYS = 365 * 2  # 2 years
_WINDOW_RE = re.compile(r"^(\d+)([wdm])$")


def _parse_window(window: str) -> int:
    """Parse a window string ("8w", "30d", "6m") into days. Raises ValueError on bad input."""
    match = _WINDOW_RE.match(window.strip().lower())
    if not match:
        raise ValueError(f"invalid window: {window!r} (use NNw, NNd, or NNm)")
    n, unit = int(match.group(1)), match.group(2)
    if n <= 0:
        raise ValueError(f"window must be positive: {window!r}")
    days = {"d": n, "w": n * 7, "m": n * 30}[unit]
    if days > MAX_WINDOW_DAYS:
        raise ValueError(f"window too large (max {MAX_WINDOW_DAYS} days)")
    return days


def _tenant_or_404(request: Request) -> Response | Any:
    tenant = getattr(request.user, "tenant", None)
    if not tenant:
        return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
    return tenant


def _validate_pillar(pillar: str) -> Response | None:
    if pillar not in ALLOWED_PILLARS:
        return Response(
            {"error": "pillar_not_available", "pillar": pillar, "allowed": sorted(ALLOWED_PILLARS)},
            status=status.HTTP_404_NOT_FOUND,
        )
    return None


def _serialize_snapshot(snap: PillarSnapshot) -> dict[str, Any]:
    return {
        "id": str(snap.id),
        "pillar": snap.pillar,
        "granularity": snap.granularity,
        "ts": snap.ts.isoformat(),
        "payload": snap.payload,
    }


class PillarHistoryView(APIView):
    """List recent snapshots for a pillar within a window."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        tenant = _tenant_or_404(request)
        if isinstance(tenant, Response):
            return tenant

        pillar = request.query_params.get("pillar", "").strip().lower()
        if err := _validate_pillar(pillar):
            return err

        granularity = request.query_params.get("granularity", "weekly").strip().lower()
        if granularity not in dict(PillarSnapshot.Granularity.choices):
            return Response(
                {"error": "invalid_granularity", "granularity": granularity},
                status=status.HTTP_400_BAD_REQUEST,
            )

        window = request.query_params.get("window", DEFAULT_WINDOW)
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


class PillarSnapshotDetailView(APIView):
    """Return a single snapshot by id (tenant-scoped)."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, snapshot_id):
        tenant = _tenant_or_404(request)
        if isinstance(tenant, Response):
            return tenant

        try:
            snap = PillarSnapshot.objects.get(id=snapshot_id, tenant=tenant)
        except PillarSnapshot.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        if err := _validate_pillar(snap.pillar):
            return err

        return Response(_serialize_snapshot(snap))


def _gravity_totals_delta(payload_a: dict, payload_b: dict) -> dict[str, str]:
    """Signed string deltas for Gravity totals (b minus a)."""

    def get_total(p: dict, key: str) -> Decimal:
        try:
            return Decimal(str(p.get("totals", {}).get(key, "0")))
        except Exception:
            return Decimal("0")

    delta = {}
    for key in ("debt", "savings", "minimum_payments"):
        d = get_total(payload_b, key) - get_total(payload_a, key)
        sign = "+" if d > 0 else ""
        delta[key] = f"{sign}{d}"
    return delta


_DIFF_FUNCS = {
    Pillar.GRAVITY.value: _gravity_totals_delta,
}


class PillarCompareView(APIView):
    """Return both snapshots plus a pillar-specific computed diff."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        tenant = _tenant_or_404(request)
        if isinstance(tenant, Response):
            return tenant

        pillar = request.query_params.get("pillar", "").strip().lower()
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
