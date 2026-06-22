"""User-facing API for the assistant baseline / insights subsystem.

Phase 1 — Read tools (snapshots):

- ``GET /api/v1/insights/history/?pillar=gravity&window=8w&granularity=weekly``
- ``GET /api/v1/insights/snapshots/<uuid>/``
- ``GET /api/v1/insights/compare/?pillar=gravity&period_a=<uuid>&period_b=<uuid>``

Phase 2 — Memory of insights (baseline + write-side):

- ``GET  /api/v1/insights/baseline/?pillar=gravity&topic=debt&window=12w``
- ``GET  /api/v1/insights/insights/?pillar=gravity&topic=debt&status=open``
- ``POST /api/v1/insights/insights/record/``
- ``POST /api/v1/insights/insights/<uuid>/confirm/``
- ``POST /api/v1/insights/insights/<uuid>/refute/``

OpenClaw runtime mirrors with X-NBHD-Internal-Key auth live in ``runtime_views.py``.

Pillar gating: only ``pillar=gravity`` is accepted; others 404 until their
snapshot pipelines ship.
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

from .baselines import compute_baseline
from .models import AssistantInsight, PillarSnapshot, TopicRegistry, UserVoicePref
from .pillars import Pillar
from .signals import compute_signals
from .topic_resolver import resolve_topic

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


# ── Phase 2: baseline + write-side ────────────────────────────────────────


MAX_INSIGHT_LIST = 100
DEFAULT_INSIGHT_LIST = 20
DEFAULT_BASELINE_WINDOW_WEEKS = 12
MAX_BASELINE_WINDOW_WEEKS = 104


def _serialize_insight(ins: AssistantInsight) -> dict[str, Any]:
    return {
        "id": str(ins.id),
        "pillar": ins.pillar,
        "topic_id": str(ins.topic_id),
        "topic_slug": ins.topic.slug if ins.topic else None,
        "statement": ins.statement,
        "evidence_refs": ins.evidence_refs,
        "confidence": ins.confidence,
        "status": ins.status,
        "created_at": ins.created_at.isoformat(),
        "last_confirmed_at": ins.last_confirmed_at.isoformat() if ins.last_confirmed_at else None,
        "last_refuted_at": ins.last_refuted_at.isoformat() if ins.last_refuted_at else None,
        "author_model_version": ins.author_model_version,
    }


def _parse_int(value, *, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


class PillarBaselineView(APIView):
    """Rolling baseline stats for a (pillar, topic) over a window. Pure stats."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        tenant = _tenant_or_404(request)
        if isinstance(tenant, Response):
            return tenant

        pillar = (request.query_params.get("pillar") or "").strip().lower()
        if err := _validate_pillar(pillar):
            return err

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


class InsightListView(APIView):
    """List AssistantInsight rows for the tenant, filterable by pillar/topic/status."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        tenant = _tenant_or_404(request)
        if isinstance(tenant, Response):
            return tenant

        qs = AssistantInsight.objects.filter(tenant=tenant).select_related("topic")

        pillar = (request.query_params.get("pillar") or "").strip().lower()
        if pillar:
            if err := _validate_pillar(pillar):
                return err
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


def _record_insight_impl(
    *,
    tenant,
    pillar: str,
    topic_input: str,
    statement: str,
    evidence_refs: dict | None,
    confidence: float | None,
    model_version: str,
) -> AssistantInsight:
    # Wrap both operations in a single atomic block so that a proposed topic
    # created by resolve_topic is never committed without the insight that
    # prompted it (prevents orphaned proposed-topic rows on insight-create failure).
    from django.db import transaction as _tx

    with _tx.atomic():
        topic = resolve_topic(pillar, topic_input, model_version=model_version)
        return AssistantInsight.objects.create(
            tenant=tenant,
            pillar=pillar,
            topic=topic,
            statement=statement,
            evidence_refs=evidence_refs or {},
            confidence=confidence if confidence is not None else 0.0,
            status=AssistantInsight.Status.OPEN,
            author_model_version=model_version,
        )


def _validate_record_body(data: dict) -> tuple[Response, None] | tuple[None, dict]:
    """Returns (error_response, None) on failure, (None, cleaned) on success."""
    pillar = (data.get("pillar") or "").strip().lower()
    if pillar not in ALLOWED_PILLARS:
        return Response(
            {"error": "pillar_not_available", "pillar": pillar, "allowed": sorted(ALLOWED_PILLARS)},
            status=status.HTTP_404_NOT_FOUND,
        ), None
    topic_input = (data.get("topic") or "").strip()
    if not topic_input:
        return Response(
            {"error": "missing_param", "required": ["topic"]},
            status=status.HTTP_400_BAD_REQUEST,
        ), None
    statement = (data.get("statement") or "").strip()
    if not statement:
        return Response(
            {"error": "missing_param", "required": ["statement"]},
            status=status.HTTP_400_BAD_REQUEST,
        ), None
    evidence = data.get("evidence_refs") or {}
    if not isinstance(evidence, dict):
        return Response(
            {"error": "invalid_param", "detail": "evidence_refs must be an object"},
            status=status.HTTP_400_BAD_REQUEST,
        ), None
    confidence = data.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            return Response(
                {"error": "invalid_param", "detail": "confidence must be numeric"},
                status=status.HTTP_400_BAD_REQUEST,
            ), None
        if not (0.0 <= confidence <= 1.0):
            return Response(
                {"error": "invalid_param", "detail": "confidence must be in [0, 1]"},
                status=status.HTTP_400_BAD_REQUEST,
            ), None
    model_version = (data.get("model_version") or "").strip()[:128]
    return None, {
        "pillar": pillar,
        "topic_input": topic_input,
        "statement": statement,
        "evidence_refs": evidence,
        "confidence": confidence,
        "model_version": model_version,
    }


class RecordInsightView(APIView):
    """Create a new AssistantInsight (status=open). Topic auto-resolves via resolve_topic."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request):
        tenant = _tenant_or_404(request)
        if isinstance(tenant, Response):
            return tenant

        err, cleaned = _validate_record_body(request.data or {})
        if err:
            return err

        ins = _record_insight_impl(tenant=tenant, **cleaned)
        return Response(_serialize_insight(ins), status=status.HTTP_201_CREATED)


def _append_user_response(ins: AssistantInsight, kind: str, note: str | None) -> None:
    entry = {"at": timezone.now().isoformat(), "kind": kind}
    if note:
        entry["note"] = note[:512]
    history = list(ins.user_responses or [])
    history.append(entry)
    ins.user_responses = history


class ConfirmInsightView(APIView):
    """Flip an insight to ``confirmed``. Idempotent — repeat confirms append to history."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, insight_id):
        tenant = _tenant_or_404(request)
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


class RefuteInsightView(APIView):
    """Flip an insight to ``refuted``. Kept on record — the assistant remembers being wrong."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, insight_id):
        tenant = _tenant_or_404(request)
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


# ── Phase 3: signals + voice prefs ────────────────────────────────────────


def _serialize_voice_pref(pref: UserVoicePref) -> dict[str, Any]:
    return {
        "id": str(pref.id),
        "pillar": pref.pillar,
        "topic_slug": pref.topic.slug if pref.topic_id else None,
        "topic_display_name": pref.topic.display_name if pref.topic_id else None,
        "scope": "topic" if pref.topic_id else "pillar",
        "register_offset": pref.register_offset,
        "tone": pref.tone,
        "volume": pref.volume,
        "created_at": pref.created_at.isoformat(),
        "updated_at": pref.updated_at.isoformat(),
    }


class PillarSignalsView(APIView):
    """Structured signals for a (pillar, topic). The LLM judges register from this."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        tenant = _tenant_or_404(request)
        if isinstance(tenant, Response):
            return tenant

        pillar = (request.query_params.get("pillar") or "").strip().lower()
        if err := _validate_pillar(pillar):
            return err

        topic_slug = (request.query_params.get("topic") or "").strip().lower()
        if not topic_slug:
            return Response(
                {"error": "missing_param", "required": ["topic"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        window_weeks = _parse_int(
            request.query_params.get("window_weeks"),
            default=12,
            lo=1,
            hi=104,
        )
        granularity = (request.query_params.get("granularity") or "weekly").strip().lower()
        if granularity not in dict(PillarSnapshot.Granularity.choices):
            return Response(
                {"error": "invalid_granularity", "granularity": granularity},
                status=status.HTTP_400_BAD_REQUEST,
            )

        signals = compute_signals(
            tenant=tenant,
            pillar=pillar,
            topic_slug=topic_slug,
            window_weeks=window_weeks,
            granularity=granularity,
        )
        return Response(signals)


def _validate_offset(value) -> int | Response:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return Response(
            {"error": "invalid_param", "detail": "register_offset must be -1, 0, or 1"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if n not in (-1, 0, 1):
        return Response(
            {"error": "invalid_param", "detail": "register_offset must be -1, 0, or 1"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return n


def _validate_voice_pref_body(data: dict) -> tuple[Response, None] | tuple[None, dict]:
    pillar = (data.get("pillar") or "").strip().lower()
    if pillar not in ALLOWED_PILLARS:
        return (
            Response(
                {"error": "pillar_not_available", "pillar": pillar, "allowed": sorted(ALLOWED_PILLARS)},
                status=status.HTTP_404_NOT_FOUND,
            ),
            None,
        )
    if "register_offset" not in data:
        return (
            Response(
                {"error": "missing_param", "required": ["register_offset"]},
                status=status.HTTP_400_BAD_REQUEST,
            ),
            None,
        )
    offset = _validate_offset(data["register_offset"])
    if isinstance(offset, Response):
        return offset, None

    tone = (data.get("tone") or "").strip().lower()
    if tone and tone not in dict(UserVoicePref.Tone.choices):
        return (
            Response(
                {"error": "invalid_param", "detail": f"tone must be one of {sorted(dict(UserVoicePref.Tone.choices))}"},
                status=status.HTTP_400_BAD_REQUEST,
            ),
            None,
        )
    volume = (data.get("volume") or "").strip().lower()
    if volume and volume not in dict(UserVoicePref.Volume.choices):
        return (
            Response(
                {
                    "error": "invalid_param",
                    "detail": f"volume must be one of {sorted(dict(UserVoicePref.Volume.choices))}",
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            None,
        )
    topic_slug = (data.get("topic") or "").strip().lower() or None
    return None, {
        "pillar": pillar,
        "topic_slug": topic_slug,
        "register_offset": offset,
        "tone": tone or None,
        "volume": volume or None,
    }


def _upsert_voice_pref_impl(
    *,
    tenant,
    pillar: str,
    topic_slug: str | None,
    register_offset: int,
    tone: str | None,
    volume: str | None,
) -> UserVoicePref:
    # Wrap both operations in a single atomic block so that a proposed topic
    # created by resolve_topic is never committed without the voice-pref row
    # that references it (mirrors the FA-0596 fix in _record_insight_impl).
    # resolve_topic's own @transaction.atomic degrades to a savepoint here, so
    # a failure in update_or_create rolls back the proposed-topic INSERT too.
    from django.db import transaction as _tx

    with _tx.atomic():
        topic = None
        if topic_slug:
            topic = TopicRegistry.objects.filter(pillar=pillar, slug=topic_slug).first()
            if topic is None:
                # Auto-resolve / propose the topic so the assistant can store an
                # override for a topic it just discovered in conversation.
                topic = resolve_topic(pillar, topic_slug)

        defaults = {"register_offset": register_offset}
        if tone:
            defaults["tone"] = tone
        if volume:
            defaults["volume"] = volume

        pref, _ = UserVoicePref.objects.update_or_create(
            tenant=tenant,
            pillar=pillar,
            topic=topic,
            defaults=defaults,
        )
    return pref


class VoicePrefSetView(APIView):
    """Persist a user-explicit voice-pref override. Idempotent upsert."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request):
        tenant = _tenant_or_404(request)
        if isinstance(tenant, Response):
            return tenant

        err, cleaned = _validate_voice_pref_body(request.data or {})
        if err:
            return err

        pref = _upsert_voice_pref_impl(tenant=tenant, **cleaned)
        return Response(_serialize_voice_pref(pref))


class VoicePrefListView(APIView):
    """List the tenant's voice-pref overrides, optionally filtered."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        tenant = _tenant_or_404(request)
        if isinstance(tenant, Response):
            return tenant

        qs = UserVoicePref.objects.filter(tenant=tenant).select_related("topic")

        pillar = (request.query_params.get("pillar") or "").strip().lower()
        if pillar:
            if err := _validate_pillar(pillar):
                return err
            qs = qs.filter(pillar=pillar)

        topic_slug = (request.query_params.get("topic") or "").strip().lower()
        if topic_slug:
            qs = qs.filter(topic__slug=topic_slug)

        rows = list(qs.order_by("pillar", "topic__slug"))
        return Response(
            {
                "count": len(rows),
                "voice_prefs": [_serialize_voice_pref(r) for r in rows],
            }
        )
