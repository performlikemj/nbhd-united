"""Tenant views."""
import logging

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Tenant
from .serializers import TenantRegistrationSerializer, TenantSerializer, UserSerializer
from apps.journal.services import seed_default_templates_for_tenant

logger = logging.getLogger(__name__)


class TenantViewSet(viewsets.ReadOnlyModelViewSet):
    """Tenant detail — users can only see their own tenant."""
    serializer_class = TenantSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if hasattr(self.request.user, "tenant"):
            return Tenant.objects.filter(id=self.request.user.tenant.id)
        return Tenant.objects.none()

    @action(detail=False, methods=["get"])
    def me(self, request):
        """Get current user's tenant."""
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response(
                {"detail": "No tenant found. Complete onboarding first."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(TenantSerializer(tenant).data)


class OnboardTenantView(APIView):
    """Create tenant during onboarding — Telegram linking happens later via QR flow."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TenantRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if hasattr(request.user, "tenant"):
            return Response(
                {"detail": "Tenant already exists."},
                status=status.HTTP_409_CONFLICT,
            )

        # Update user profile
        user = request.user
        user.display_name = serializer.validated_data.get("display_name", user.display_name)
        user.language = serializer.validated_data.get("language", user.language)
        user.timezone = serializer.validated_data.get("timezone", user.timezone)
        user.preferences = {
            **user.preferences,
            "agent_persona": serializer.validated_data.get("agent_persona", "neighbor"),
        }
        user.save(update_fields=["display_name", "language", "timezone", "preferences"])

        # Create tenant
        tenant = Tenant.objects.create(user=user)
        seed_default_templates_for_tenant(tenant=tenant)
        return Response(TenantSerializer(tenant).data, status=status.HTTP_201_CREATED)


class PersonaListView(APIView):
    """List available agent personas."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.orchestrator.personas import list_personas
        return Response(list_personas())


class RefreshConfigView(APIView):
    """Allow users to refresh their OpenClaw agent configuration."""
    permission_classes = [IsAuthenticated]

    # 5 minute cooldown
    COOLDOWN_SECONDS = 300

    def get(self, request):
        """Return current refresh status."""
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "can_refresh": self._can_refresh(tenant),
            "last_refreshed": tenant.config_refreshed_at,
            "cooldown_seconds": self.COOLDOWN_SECONDS,
            "status": tenant.status,
            "has_pending_update": tenant.pending_config_version > tenant.config_version,
        })

    def post(self, request):
        """Trigger a config refresh."""
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        if tenant.status != Tenant.Status.ACTIVE:
            return Response(
                {"detail": "Agent is not active."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not self._can_refresh(tenant):
            return Response(
                {"detail": "Please wait before refreshing again.", "cooldown_seconds": self.COOLDOWN_SECONDS},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            from apps.orchestrator.services import update_tenant_config
            update_tenant_config(str(tenant.id))

            now = timezone.now()
            tenant.config_refreshed_at = now
            tenant.config_version = tenant.pending_config_version
            tenant.save(update_fields=["config_refreshed_at", "config_version"])

            return Response({
                "detail": "Configuration refreshed. Your assistant will restart momentarily.",
                "last_refreshed": now,
            })
        except Exception:
            logger.exception("Config refresh failed for tenant %s", tenant.id)
            return Response(
                {"detail": "Refresh failed. Please try again later."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

    def _can_refresh(self, tenant):
        if not tenant.config_refreshed_at:
            return True
        elapsed = (timezone.now() - tenant.config_refreshed_at).total_seconds()
        return elapsed >= self.COOLDOWN_SECONDS


class UpdatePreferencesView(APIView):
    """Update user preferences (e.g. agent persona)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "agent_persona": request.user.preferences.get("agent_persona", "neighbor"),
        })

    def patch(self, request):
        from apps.orchestrator.personas import PERSONAS

        persona = request.data.get("agent_persona")
        if persona is not None:
            if persona not in PERSONAS:
                return Response(
                    {"detail": f"Unknown persona: {persona}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            request.user.preferences = {
                **request.user.preferences,
                "agent_persona": persona,
            }
            request.user.save(update_fields=["preferences"])

        return Response({
            "agent_persona": request.user.preferences.get("agent_persona", "neighbor"),
        })


class ProfileView(APIView):
    """Get/update current user's profile fields."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        original_timezone = request.user.timezone
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        if serializer.validated_data.get("timezone") and request.user.timezone != original_timezone:
            try:
                tenant = request.user.tenant
            except Tenant.DoesNotExist:
                tenant = None
            if tenant and tenant.status == Tenant.Status.ACTIVE and tenant.container_id:
                from apps.orchestrator.services import update_tenant_config
                try:
                    update_tenant_config(str(tenant.id))
                except Exception:
                    logger.exception(
                        "Failed to refresh OpenClaw config after timezone update for tenant %s",
                        tenant.id,
                    )

        return Response(serializer.data)
