"""Tenant views."""
import logging

from datetime import timedelta

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Tenant
from .serializers import TenantRegistrationSerializer, TenantSerializer, UserSerializer
from apps.cron.publish import publish_task
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
            tenant = request.user.tenant
            if tenant.status in {Tenant.Status.PENDING, Tenant.Status.PROVISIONING}:
                return Response(
                    {
                        "detail": "Provisioning is still in progress. Please wait a moment and refresh.",
                        "tenant_status": tenant.status,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                {"detail": "Tenant already exists.", "tenant_status": tenant.status},
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

        # Create tenant and provision immediately for 7-day free trial
        now = timezone.now()
        tenant = Tenant.objects.create(
            user=user,
            is_trial=True,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=7),
            model_tier=Tenant.ModelTier.STARTER,
            status=Tenant.Status.PROVISIONING,
        )
        logger.info(
            "tenant_provisioning tenant_id=%s user_id=%s stage=onboarding_tenant_created error=",
            tenant.id,
            user.id,
        )
        seed_default_templates_for_tenant(tenant=tenant)

        try:
            publish_task("provision_tenant", str(tenant.id))
            logger.info(
                "tenant_provisioning tenant_id=%s user_id=%s stage=publish_provision_task error=",
                tenant.id,
                user.id,
            )
        except Exception as exc:
            tenant.status = Tenant.Status.PENDING
            tenant.save(update_fields=["status", "updated_at"])
            logger.exception(
                "tenant_provisioning tenant_id=%s user_id=%s stage=publish_provision_task_failed error=%s",
                tenant.id,
                user.id,
                exc,
            )
            return Response(
                {
                    "detail": "Signup succeeded, but provisioning could not be started. Please retry shortly.",
                    "tenant_status": tenant.status,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(TenantSerializer(tenant).data, status=status.HTTP_201_CREATED)


class ProvisioningStatusView(APIView):
    """Return tenant provisioning readiness for the authenticated user."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        has_container_id = bool(tenant.container_id)
        has_container_fqdn = bool(tenant.container_fqdn)
        ready = bool(
            tenant.status == Tenant.Status.ACTIVE
            and has_container_id
            and has_container_fqdn
        )

        return Response({
            "tenant_id": str(tenant.id),
            "user_id": str(request.user.id),
            "status": tenant.status,
            "container_id": tenant.container_id,
            "container_fqdn": tenant.container_fqdn,
            "has_container_id": has_container_id,
            "has_container_fqdn": has_container_fqdn,
            "provisioned_at": tenant.provisioned_at,
            "created_at": tenant.created_at,
            "updated_at": tenant.updated_at,
            "ready": ready,
        })


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
            if tenant.status in {Tenant.Status.PENDING, Tenant.Status.PROVISIONING}:
                return Response(
                    {
                        "detail": "Provisioning is in progress. Try again once your assistant is ready.",
                        "tenant_status": tenant.status,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                {"detail": "Agent is not active.", "tenant_status": tenant.status},
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

            try:
                tenant = request.user.tenant
                if tenant.status == Tenant.Status.ACTIVE:
                    tenant.bump_pending_config()
            except Tenant.DoesNotExist:
                pass

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
                if tenant and tenant.status == Tenant.Status.ACTIVE:
                    tenant.bump_pending_config()
                    if tenant.container_id:
                        from apps.orchestrator.services import update_tenant_config
                        try:
                            update_tenant_config(str(tenant.id))
                        except Exception:
                            logger.exception(
                                "Failed to refresh config after tz update for tenant %s",
                                tenant.id,
                            )
            except Tenant.DoesNotExist:
                pass

        return Response(serializer.data)
