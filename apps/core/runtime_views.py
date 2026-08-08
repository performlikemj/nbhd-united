"""Internal runtime views for the OpenClaw core (mindfulness) plugin.

Authenticated by the shared internal key + tenant-id header (AllowAny + manual
validation), exactly like the fuel/finance runtime surfaces. The assistant
authors a render manifest and POSTs it here; the backend validates, persists a
pending MeditationSession, and enqueues the async render.
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.router.document_write_guard import record_runtime_write_activity
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

from .models import CoreProfile, MeditationSession, MeditationStatus

logger = logging.getLogger(__name__)

_PROFILE_FIELDS = (
    "onboarding_status",
    "preferred_voice",
    "preferred_duration_minutes",
    "ambient_bed_enabled",
    "daily_cron_enabled",
    "preferred_time",
    "additional_context",
)


def _is_recoverable_render_failure(session: MeditationSession) -> bool:
    """Return whether a failed session can safely resume its existing render."""
    if (
        session.status != MeditationStatus.FAILED
        or session.failure_class != "transient"
        or session.attempt_count >= settings.CORE_RENDER_MAX_ATTEMPTS
    ):
        return False

    from apps.core import render as core_render

    return not core_render.validate_manifest(session.manifest)


def _record_dispatch_error(session: MeditationSession, exc: Exception) -> None:
    """Leave a durable marker for the reaper when QStash publish fails."""
    error = f"dispatch_error: {exc}"
    updated = MeditationSession.objects.filter(
        id=session.id,
        status=MeditationStatus.PENDING,
        error=session.error,
        failure_class=session.failure_class,
    ).update(
        error=error,
        failure_class="transient",
        updated_at=timezone.now(),
    )
    if updated:
        session.error = error
        session.failure_class = "transient"
    else:
        # A publish timeout is ambiguous: QStash may have accepted and already
        # delivered the task. Never overwrite a worker's progressed state.
        session.refresh_from_db()


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


class RuntimeCoreSummaryView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, tenant_id: UUID):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        ready = MeditationSession.objects.filter(tenant=tenant, status=MeditationStatus.READY)
        last = ready.order_by("-date", "-created_at").first()
        return Response(
            {
                "total_sessions": ready.count(),
                "last": ({"id": str(last.id), "title": last.title, "date": last.date.isoformat()} if last else None),
            }
        )


class RuntimeCoreProfileView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, tenant_id: UUID):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        profile, _created = CoreProfile.objects.get_or_create(tenant=tenant)
        return Response({f: getattr(profile, f) for f in _PROFILE_FIELDS})

    def patch(self, request, tenant_id: UUID):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        record_runtime_write_activity(tenant)
        profile, _created = CoreProfile.objects.get_or_create(tenant=tenant)
        changed = []
        incoming = {}
        for field in _PROFILE_FIELDS:
            if field in request.data:
                incoming[field] = request.data[field]
                changed.append(field)
        if changed:
            from apps.pii.store_authoring import author_store_fields

            authored, receipts = author_store_fields(
                tenant,
                incoming,
                model_label="core.CoreProfile",
                seam="core.runtime.profile.update",
                writer="runtime",
                receipts=profile.pii_receipts,
            )
            for field, value in authored.items():
                setattr(profile, field, value)
            profile.pii_receipts = receipts
            profile.save(update_fields=[*changed, "pii_receipts", "updated_at"])
        return Response({f: getattr(profile, f) for f in _PROFILE_FIELDS})


class RuntimeMeditationCreateView(APIView):
    """POST: the assistant submits an authored render manifest.

    Fully validates the manifest against the fixed phase arc + timing bounds
    (rejecting bad input before any TTS spend), persists a pending
    MeditationSession, and enqueues the async render task.
    """

    permission_classes = [AllowAny]

    def post(self, request, tenant_id: UUID):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        from apps.core import render as core_render

        manifest = request.data.get("manifest")
        if not isinstance(manifest, dict):
            return Response(
                {"error": "invalid_manifest", "detail": "manifest must be a JSON object"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        manifest_errors = core_render.validate_manifest(manifest)
        if manifest_errors:
            return Response(
                {"error": "invalid_manifest", "detail": manifest_errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.common.tenant_tz import tenant_today

        today = tenant_today(tenant)
        response_status = status.HTTP_201_CREATED

        in_flight = (
            MeditationSession.objects.filter(
                tenant=tenant,
                date=today,
                status__in=[MeditationStatus.PENDING, MeditationStatus.RENDERING],
            )
            .order_by("-created_at")
            .first()
        )
        if in_flight:
            return Response({"meditation_id": str(in_flight.id), "status": in_flight.status})

        record_runtime_write_activity(tenant)

        from apps.pii.store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {
                "title": str(manifest.get("title", ""))[:160],
                "theme": str(manifest.get("theme", "")),
                "manifest": manifest,
            },
            model_label="core.MeditationSession",
            seam="core.runtime.meditation.create",
            writer="runtime",
        )

        # Serialize creation per tenant so repeated runtime calls cannot create
        # a second billable render while today's first request is still active.
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=tenant.pk)

            in_flight = (
                MeditationSession.objects.select_for_update()
                .filter(
                    tenant=tenant,
                    date=today,
                    status__in=[MeditationStatus.PENDING, MeditationStatus.RENDERING],
                )
                .order_by("-created_at")
                .first()
            )
            if in_flight:
                return Response({"meditation_id": str(in_flight.id), "status": in_flight.status})

            latest_today = (
                MeditationSession.objects.select_for_update()
                .filter(tenant=tenant, date=today)
                .order_by("-created_at")
                .first()
            )
            if latest_today and _is_recoverable_render_failure(latest_today):
                session = latest_today
                session.status = MeditationStatus.PENDING
                session.save(update_fields=["status", "updated_at"])
                response_status = status.HTTP_200_OK
            else:
                session = MeditationSession.objects.create(
                    tenant=tenant,
                    date=today,  # the user's LOCAL day, not server UTC
                    status=MeditationStatus.PENDING,
                    title=authored["title"],
                    theme=authored["theme"],
                    voice=str(manifest.get("voice", "")),
                    manifest=authored["manifest"],
                    pii_receipts=receipts,
                )

        try:
            from apps.cron.publish import publish_task

            publish_task("render_meditation", str(session.id))
        except Exception as exc:
            _record_dispatch_error(session, exc)
            logger.exception("Failed to enqueue render for meditation %s", session.id)

        return Response(
            {"meditation_id": str(session.id), "status": session.status},
            status=response_status,
        )


class RuntimeMeditationDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, tenant_id: UUID, meditation_id: UUID):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        try:
            session = MeditationSession.objects.get(id=meditation_id, tenant_id=tenant_id)
        except MeditationSession.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {
                "id": str(session.id),
                "status": session.status,
                "title": session.title,
                "audio_url": session.audio_url,
                "duration_ms": session.duration_ms,
            }
        )
