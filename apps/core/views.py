"""Consumer-facing Core API views (JWT auth, frontend)."""

import logging
from datetime import date

from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CoreProfile, MeditationSession, MeditationStatus
from .serializers import CoreProfileSerializer, MeditationSessionSerializer

_logger = logging.getLogger(__name__)

_CORE_WELCOME_PROMPT_TEMPLATE = (
    "Core (mindfulness) was just enabled for this user. Send them a brief, warm "
    "welcome via `nbhd_send_to_user` letting them know their meditation space is "
    "ready — whenever they want a quiet ten minutes, you'll compose a guided "
    "meditation for them, drawn from what you've learned about their week. Keep "
    "it to 2-3 sentences. Don't start any questionnaire — just let them know it's "
    "live and on demand.\n\n"
    "**This is a TOOL invocation, not a chat message.** You MUST actually call "
    "`nbhd_send_to_user` to deliver the welcome. Writing the text in your reply "
    "without calling the tool does NOT send anything to the user.\n\n"
    "**Do NOT ask questions in this message.**\n\n"
    "**After `nbhd_send_to_user` succeeds**, mark the welcome as delivered so the "
    "deploy-time backfill knows not to re-send. Run via Bash:\n"
    "  curl -fsS -X POST \\\n"
    '    "$NBHD_API_BASE_URL/api/v1/tenants/runtime/{tenant_id}/welcomes/core/" \\\n'
    '    -H "X-NBHD-Internal-Key: $NBHD_INTERNAL_API_KEY" \\\n'
    '    -H "X-NBHD-Tenant-Id: {tenant_id}"\n\n'
    "If `nbhd_send_to_user` returned an error (timeout, channel rejection, etc.), "
    "DO NOT run the curl — leave the welcome unmarked so the next deploy retries."
)


def _schedule_core_welcome(tenant):
    """Create a one-shot cron that sends a Core welcome message (~90s post-restart)."""
    from apps.orchestrator.welcome_scheduler import schedule_welcome

    return schedule_welcome(
        tenant,
        feature="core",
        cron_name="_core:welcome",
        prompt_template=_CORE_WELCOME_PROMPT_TEMPLATE,
    )


class CoreSettingsView(APIView):
    """PATCH: toggle core_enabled for the tenant.

    Enabling Core adds the mindfulness plugin (requires assistant restart). The
    response includes ``restart_required: true`` so the frontend can confirm
    before calling ``POST /api/v1/core/restart/``.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        core_enabled = request.data.get("core_enabled")
        if core_enabled is None:
            return Response({"error": "core_enabled is required"}, status=status.HTTP_400_BAD_REQUEST)

        was_enabled = tenant.core_enabled
        tenant.core_enabled = bool(core_enabled)
        update_fields = ["core_enabled"]

        # Fresh enable (off → on): clear the welcome marker so it re-fires.
        if tenant.core_enabled and not was_enabled:
            marks = dict(tenant.welcomes_sent or {})
            if marks.pop("core", None) is not None:
                tenant.welcomes_sent = marks
                update_fields.append("welcomes_sent")

        tenant.save(update_fields=update_fields)
        tenant.bump_pending_config()

        profile_status = None
        if tenant.core_enabled:
            profile, _created = CoreProfile.objects.get_or_create(tenant=tenant)
            profile_status = profile.onboarding_status

        try:
            from apps.cron.publish import publish_task

            publish_task("apply_single_tenant_config", str(tenant.id))
        except Exception:
            _logger.warning("Failed to enqueue config deploy for tenant %s", tenant.id)

        plugin_changed = was_enabled != tenant.core_enabled
        restart_required = plugin_changed and bool(tenant.container_id)

        return Response(
            {
                "core_enabled": tenant.core_enabled,
                "core_profile_status": profile_status,
                "restart_required": restart_required,
            }
        )


class CoreRestartView(APIView):
    """POST: restart the assistant to pick up plugin changes."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        if not tenant.container_id:
            return Response({"error": "no_container"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from apps.orchestrator.azure_client import restart_container_app

            restart_container_app(tenant.container_id)
        except Exception:
            _logger.exception("Container restart failed for tenant %s", tenant.id)
            return Response(
                {"error": "restart_failed", "detail": "Could not restart your assistant. Try again in a moment."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Schedule welcome AFTER the container is back up (~90s cold start).
        if tenant.core_enabled:
            try:
                profile = CoreProfile.objects.get(tenant=tenant)
                if profile.onboarding_status == "pending":
                    from apps.cron.publish import publish_task

                    publish_task("schedule_core_welcome", str(tenant.id), delay_seconds=90)
            except CoreProfile.DoesNotExist:
                pass

        return Response({"restarted": True})


class CoreProfileView(APIView):
    """GET / PATCH the tenant's CoreProfile."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        profile, _created = CoreProfile.objects.get_or_create(tenant=tenant)
        return Response(CoreProfileSerializer(profile).data)

    def patch(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        profile, _created = CoreProfile.objects.get_or_create(tenant=tenant)
        serializer = CoreProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class MeditationSessionListView(ListAPIView):
    """GET the tenant's meditations (the library). Defaults to ready sessions."""

    permission_classes = [IsAuthenticated]
    serializer_class = MeditationSessionSerializer

    def get_queryset(self):
        tenant = getattr(self.request.user, "tenant", None)
        if not tenant:
            return MeditationSession.objects.none()
        qs = MeditationSession.objects.filter(tenant=tenant)
        requested = self.request.query_params.get("status")
        if requested:
            qs = qs.filter(status=requested)
        else:
            qs = qs.filter(status=MeditationStatus.READY)
        return qs


class MeditationSessionDetailView(RetrieveAPIView):
    """GET a single meditation by id (tenant-scoped)."""

    permission_classes = [IsAuthenticated]
    serializer_class = MeditationSessionSerializer
    lookup_field = "id"

    def get_queryset(self):
        tenant = getattr(self.request.user, "tenant", None)
        if not tenant:
            return MeditationSession.objects.none()
        return MeditationSession.objects.filter(tenant=tenant)


class CoreComposeView(APIView):
    """POST: compose-on-demand (the web orb).

    Creates a pending MeditationSession and enqueues the async compose task (LLM
    authors a manifest → render). Returns the meditation id for the frontend to
    poll via ``GET /sessions/<id>/``. Coalesces a mashed orb: if a compose is
    already in flight, returns that one instead of spending a second render.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        if not getattr(tenant, "core_enabled", False):
            return Response({"error": "core_not_enabled"}, status=status.HTTP_403_FORBIDDEN)

        in_flight = (
            MeditationSession.objects.filter(
                tenant=tenant, status__in=[MeditationStatus.PENDING, MeditationStatus.RENDERING]
            )
            .order_by("-created_at")
            .first()
        )
        if in_flight:
            return Response({"meditation_id": str(in_flight.id), "status": in_flight.status})

        session = MeditationSession.objects.create(
            tenant=tenant,
            date=date.today(),
            status=MeditationStatus.PENDING,
        )
        try:
            from apps.cron.publish import publish_task

            publish_task("compose_meditation", str(session.id))
        except Exception:
            _logger.warning("Failed to enqueue compose for meditation %s", session.id)

        return Response(
            {"meditation_id": str(session.id), "status": session.status},
            status=status.HTTP_201_CREATED,
        )
