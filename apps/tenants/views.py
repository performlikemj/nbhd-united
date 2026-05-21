"""Tenant views."""

import logging
from datetime import UTC, datetime, timedelta

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.cache import tenant_cache
from apps.cron.publish import publish_task
from apps.journal.services import seed_default_templates_for_tenant
from apps.orchestrator.config_generator import TIER_MODEL_CONFIGS

from .models import Tenant
from .serializers import HeartbeatConfigSerializer, TenantRegistrationSerializer, TenantSerializer, UserSerializer

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
    @tenant_cache(ttl=60, tag="tenant")
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

        # Create tenant and provision immediately for free trial
        # TODO: revert to timedelta(days=7) after March 2026 promotion ends
        now = timezone.now()
        trial_end = datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)
        tenant = Tenant.objects.create(
            user=user,
            is_trial=True,
            trial_started_at=now,
            trial_ends_at=max(now + timedelta(days=7), trial_end),
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
        ready = bool(tenant.status == Tenant.Status.ACTIVE and has_container_id and has_container_fqdn)

        return Response(
            {
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
            }
        )


class RetryProvisioningView(APIView):
    """Allow authenticated users to re-trigger tenant provisioning safely."""

    permission_classes = [IsAuthenticated]
    RETRY_COOLDOWN_SECONDS = 90

    def post(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        has_container_id = bool(tenant.container_id)
        has_container_fqdn = bool(tenant.container_fqdn)
        is_ready = bool(tenant.status == Tenant.Status.ACTIVE and has_container_id and has_container_fqdn)
        if is_ready:
            return Response(
                {
                    "detail": "Your assistant is already active.",
                    "tenant_status": tenant.status,
                    "ready": True,
                },
                status=status.HTTP_200_OK,
            )

        if tenant.status in {Tenant.Status.SUSPENDED, Tenant.Status.DEPROVISIONING, Tenant.Status.DELETED}:
            return Response(
                {
                    "detail": "Provisioning retry is unavailable for this tenant state.",
                    "tenant_status": tenant.status,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        elapsed = (timezone.now() - tenant.updated_at).total_seconds()
        if tenant.status == Tenant.Status.PROVISIONING and elapsed < self.RETRY_COOLDOWN_SECONDS:
            return Response(
                {
                    "detail": "Provisioning is already in progress. Please wait a moment before retrying.",
                    "tenant_status": tenant.status,
                    "retry_after_seconds": int(self.RETRY_COOLDOWN_SECONDS - elapsed),
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        tenant.status = Tenant.Status.PROVISIONING
        tenant.save(update_fields=["status", "updated_at"])

        try:
            publish_task("provision_tenant", str(tenant.id))
            logger.info(
                "tenant_provisioning tenant_id=%s user_id=%s stage=user_retry_queued",
                tenant.id,
                request.user.id,
            )
        except Exception as exc:
            tenant.status = Tenant.Status.PENDING
            tenant.save(update_fields=["status", "updated_at"])
            logger.exception(
                "tenant_provisioning tenant_id=%s user_id=%s stage=user_retry_publish_failed error=%s",
                tenant.id,
                request.user.id,
                exc,
            )
            return Response(
                {
                    "detail": "Could not queue provisioning retry right now. Please try again shortly.",
                    "tenant_status": tenant.status,
                    "ready": False,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "detail": "Provisioning retry queued. We will keep setting up your assistant in the background.",
                "tenant_status": tenant.status,
                "ready": False,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class PersonaListView(APIView):
    """List available agent personas."""

    permission_classes = [IsAuthenticated]

    @tenant_cache(ttl=300, tag="tenant")
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

        from django.conf import settings as django_settings

        latest_tag = getattr(django_settings, "OPENCLAW_IMAGE_TAG", None)
        running_tag = tenant.container_image_tag or None
        image_outdated = bool(latest_tag and running_tag and latest_tag != "latest" and latest_tag != running_tag)
        return Response(
            {
                "can_refresh": self._can_refresh(tenant),
                "last_refreshed": tenant.config_refreshed_at,
                "cooldown_seconds": self.COOLDOWN_SECONDS,
                "status": tenant.status,
                "has_pending_update": tenant.pending_config_version > tenant.config_version,
                "container_image_tag": running_tag,
                "latest_image_tag": latest_tag,
                "image_outdated": image_outdated,
            }
        )

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

            return Response(
                {
                    "detail": "Configuration refreshed. Your assistant will restart momentarily.",
                    "last_refreshed": now,
                }
            )
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


class HeartbeatConfigView(APIView):
    """Get/update heartbeat window and proactive assistant settings."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response(
                {"detail": "No tenant found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "enabled": tenant.heartbeat_enabled,
                "start_hour": tenant.heartbeat_start_hour,
                "window_hours": tenant.heartbeat_window_hours,
                "feature_tips": tenant.feature_tips_enabled,
            }
        )

    def patch(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response(
                {"detail": "No tenant found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = HeartbeatConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        update_fields = []
        if "enabled" in data:
            tenant.heartbeat_enabled = data["enabled"]
            update_fields.append("heartbeat_enabled")
        if "start_hour" in data:
            tenant.heartbeat_start_hour = data["start_hour"]
            update_fields.append("heartbeat_start_hour")
        if "feature_tips" in data:
            tenant.feature_tips_enabled = data["feature_tips"]
            update_fields.append("feature_tips_enabled")

        applied_state = "ok"

        if update_fields:
            tenant.full_clean()
            update_fields.append("updated_at")
            tenant.save(update_fields=update_fields)

            if tenant.status == Tenant.Status.ACTIVE:
                from apps.orchestrator.hibernation import (
                    DEFERRED,
                    apply_or_defer_gateway_call,
                )
                from apps.orchestrator.services import (
                    sync_heartbeat_cron,
                    update_tenant_config,
                )

                # Bump pending once so the apply_pending_configs scheduler
                # reconciles drift if a synchronous call below fails for a
                # non-availability reason. The deferral helper does not bump.
                tenant.bump_pending_config()

                try:
                    result = apply_or_defer_gateway_call(
                        tenant,
                        lambda: update_tenant_config(str(tenant.id)),
                        label="heartbeat.update_tenant_config",
                    )
                    if result is DEFERRED:
                        applied_state = "pending"
                except Exception:
                    logger.exception(
                        "Failed to push config for tenant %s (will apply on next cycle)",
                        tenant.id,
                    )

                if any(f in ("heartbeat_enabled", "heartbeat_start_hour") for f in update_fields):
                    try:
                        result = apply_or_defer_gateway_call(
                            tenant,
                            lambda: sync_heartbeat_cron(tenant),
                            label="heartbeat.sync_heartbeat_cron",
                        )
                        if result is DEFERRED:
                            applied_state = "pending"
                    except Exception:
                        logger.exception(
                            "Failed to sync heartbeat cron for tenant %s",
                            tenant.id,
                        )

        return Response(
            {
                "enabled": tenant.heartbeat_enabled,
                "start_hour": tenant.heartbeat_start_hour,
                "window_hours": tenant.heartbeat_window_hours,
                "feature_tips": tenant.feature_tips_enabled,
                "applied": applied_state,
            }
        )


class UpdatePreferencesView(APIView):
    """Update user preferences (e.g. agent persona)."""

    permission_classes = [IsAuthenticated]

    @tenant_cache(ttl=120, tag="tenant")
    def get(self, request):
        return Response(
            {
                "agent_persona": request.user.preferences.get("agent_persona", "neighbor"),
            }
        )

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

        return Response(
            {
                "agent_persona": request.user.preferences.get("agent_persona", "neighbor"),
            }
        )


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

        applied_state = "ok"

        location_changed = any(
            k in serializer.validated_data for k in ("location_city", "location_lat", "location_lon")
        )
        if location_changed:
            try:
                tenant = request.user.tenant
                if tenant and tenant.status == Tenant.Status.ACTIVE:
                    tenant.bump_pending_config()
                    if tenant.container_id:
                        from apps.orchestrator.hibernation import (
                            DEFERRED,
                            apply_or_defer_gateway_call,
                        )
                        from apps.orchestrator.services import update_tenant_config

                        try:
                            result = apply_or_defer_gateway_call(
                                tenant,
                                lambda: update_tenant_config(str(tenant.id)),
                                label="profile.location.update_tenant_config",
                            )
                            if result is DEFERRED:
                                applied_state = "pending"
                        except Exception:
                            logger.exception(
                                "Failed to refresh config after location update for tenant %s",
                                tenant.id,
                            )
            except Tenant.DoesNotExist:
                pass

        if serializer.validated_data.get("timezone") and request.user.timezone != original_timezone:
            try:
                tenant = request.user.tenant
                if tenant and tenant.status == Tenant.Status.ACTIVE:
                    tenant.bump_pending_config()
                    if tenant.container_id:
                        from apps.orchestrator.hibernation import (
                            DEFERRED,
                            apply_or_defer_gateway_call,
                        )
                        from apps.orchestrator.services import update_tenant_config

                        try:
                            result = apply_or_defer_gateway_call(
                                tenant,
                                lambda: update_tenant_config(str(tenant.id)),
                                label="profile.timezone.update_tenant_config",
                            )
                            if result is DEFERRED:
                                applied_state = "pending"
                        except Exception:
                            logger.exception(
                                "Failed to refresh config after tz update for tenant %s",
                                tenant.id,
                            )

                        # Sync timezone on all existing cron jobs
                        def _sync_cron_timezones() -> None:
                            from apps.cron.gateway_client import invoke_gateway_tool

                            new_tz = request.user.timezone
                            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
                            jobs: list = []
                            if isinstance(list_result, dict):
                                jobs = list_result.get("jobs", [])
                            elif isinstance(list_result, list):
                                jobs = list_result
                            for job in jobs:
                                job_id = job.get("jobId") or job.get("name")
                                schedule = job.get("schedule", {})
                                if schedule.get("tz") != new_tz:
                                    invoke_gateway_tool(
                                        tenant,
                                        "cron.update",
                                        {
                                            "jobId": job_id,
                                            "patch": {"schedule": {**schedule, "tz": new_tz}},
                                        },
                                    )
                            logger.info(
                                "Synced %d cron job timezone(s) to %s for tenant %s",
                                len(jobs),
                                new_tz,
                                tenant.id,
                            )

                        try:
                            result = apply_or_defer_gateway_call(
                                tenant,
                                _sync_cron_timezones,
                                label="profile.timezone.cron_sweep",
                            )
                            if result is DEFERRED:
                                applied_state = "pending"
                        except Exception:
                            logger.exception(
                                "Failed to sync cron timezones for tenant %s",
                                tenant.id,
                            )
            except Tenant.DoesNotExist:
                pass

        response_data = dict(serializer.data)
        response_data["applied"] = applied_state
        return Response(response_data)


def _do_hard_delete(user) -> None:
    """Deprovision tenant and hard-delete the user. Called immediately (no
    subscription) or from the Stripe webhook when the subscription ends."""
    import logging as _logging

    _log = _logging.getLogger(__name__)

    tenant = getattr(user, "tenant", None)
    if tenant and tenant.status not in ("deleted", "deprovisioning"):
        try:
            from apps.orchestrator.services import deprovision_tenant

            deprovision_tenant(str(tenant.id))
            _log.info("Deprovisioned tenant %s for user %s", tenant.id, user.id)
        except Exception:
            _log.warning(
                "Could not deprovision tenant %s during deletion — continuing",
                tenant.id,
                exc_info=True,
            )

    user_id, user_email = user.id, user.email
    user.delete()
    _log.info("Hard-deleted account: user_id=%s email=%s", user_id, user_email)


class DeleteAccountView(APIView):
    """Schedule permanent deletion of the authenticated user's account.

    Behaviour:
    - Active Stripe subscription → cancel at period end; account stays alive
      and fully functional until then; ``customer.subscription.deleted`` webhook
      triggers the actual hard-delete.
    - No active subscription → hard-delete immediately.

    Requires { "confirm": "DELETE" } in the request body.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if request.data.get("confirm") != "DELETE":
            return Response(
                {"detail": 'Send {"confirm": "DELETE"} to confirm.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        tenant = getattr(user, "tenant", None)

        # Already scheduled — idempotent
        if tenant and tenant.pending_deletion:
            return Response(
                {
                    "scheduled": True,
                    "deletion_scheduled_at": tenant.deletion_scheduled_at,
                    "detail": "Deletion already scheduled.",
                },
                status=status.HTTP_200_OK,
            )

        has_active_sub = bool(tenant and tenant.stripe_subscription_id)

        if has_active_sub:
            # ── Has subscription: cancel at period end, schedule deletion ──────
            period_end = None
            try:
                import stripe
                from django.conf import settings as dj_settings

                stripe.api_key = (
                    dj_settings.STRIPE_LIVE_SECRET_KEY
                    if getattr(dj_settings, "STRIPE_LIVE_MODE", False)
                    else dj_settings.STRIPE_TEST_SECRET_KEY
                )
                sub = stripe.Subscription.modify(
                    tenant.stripe_subscription_id,
                    cancel_at_period_end=True,
                )
                import datetime

                period_end = datetime.datetime.fromtimestamp(sub["current_period_end"], tz=datetime.UTC)
                logger.info(
                    "Subscription %s set to cancel at period end %s for user %s",
                    tenant.stripe_subscription_id,
                    period_end,
                    user.id,
                )
            except Exception:
                logger.warning(
                    "Could not cancel Stripe subscription for user %s — scheduling deletion anyway",
                    user.id,
                    exc_info=True,
                )

            tenant.pending_deletion = True
            tenant.deletion_scheduled_at = period_end
            tenant.save(update_fields=["pending_deletion", "deletion_scheduled_at", "updated_at"])

            return Response(
                {
                    "scheduled": True,
                    "deletion_scheduled_at": period_end,
                    "detail": (
                        "Your account is scheduled for deletion at the end of your billing period. "
                        "You have full access until then."
                    ),
                },
                status=status.HTTP_200_OK,
            )

        else:
            # ── No subscription: hard-delete immediately ──────────────────────
            user_id = user.id
            try:
                _do_hard_delete(user)
            except Exception:
                logger.exception("Hard-delete failed for user %s", user_id)
                return Response(
                    {"detail": "Deletion failed. Please contact support."},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            return Response({"scheduled": False, "detail": "Account deleted."}, status=status.HTTP_200_OK)


class PreferredModelView(APIView):
    """Set the user's preferred primary model within their tier."""

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        tenant = request.user.tenant
        model_id = request.data.get("preferred_model", "")

        allowed = _get_allowed_models(tenant)
        if model_id and model_id not in allowed:
            return Response(
                {"error": "Model not available for your tier"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant.preferred_model = model_id
        tenant.save(update_fields=["preferred_model"])
        tenant.bump_pending_config()
        _enqueue_immediate_apply(tenant)

        return Response(
            {
                "preferred_model": model_id,
                "model_tier": tenant.model_tier,
            }
        )


def _get_allowed_models(tenant: Tenant) -> dict:
    """Return the set of model IDs allowed for this tenant's tier, plus
    any BYO subscription extras the tenant has connected."""
    from apps.orchestrator.config_generator import _byo_model_extras

    base = TIER_MODEL_CONFIGS.get(tenant.model_tier, {})
    extras = _byo_model_extras(tenant)
    if extras:
        return {**base, **extras}
    return base


def _enqueue_immediate_apply(tenant: Tenant) -> None:
    """Fire-and-forget: enqueue an apply_single_tenant_config task right now
    so picker/per-task model changes land within ~5–30s instead of waiting
    for the hourly apply-pending-configs cron (which also skips actively-
    chatting tenants via a 15-min idle filter).

    Idempotency-keyed so rapid clicks coalesce. Publish failure is logged
    but never raised — the hourly cron is the safety net.
    """
    if not tenant.container_id or tenant.status != Tenant.Status.ACTIVE:
        return
    if tenant.hibernated_at:
        return
    try:
        from apps.cron.publish import publish_task

        publish_task(
            "apply_single_tenant_config",
            str(tenant.id),
            idempotency_key=f"apply-config-{tenant.id}",
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "Failed to enqueue immediate apply for tenant %s — falling back to hourly cron",
            str(tenant.id)[:8],
            exc_info=True,
        )


_VALID_TASK_SLUGS = {
    "heartbeat",
    "morning_briefing",
    "evening_checkin",
    "week_review",
    "background_tasks",
}


class TaskModelPreferencesView(APIView):
    """Set per-task model overrides for scheduled jobs."""

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        tenant = request.user.tenant
        prefs = request.data.get("task_model_preferences", {})

        if not isinstance(prefs, dict):
            return Response(
                {"error": "task_model_preferences must be an object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        allowed = _get_allowed_models(tenant)
        for slug, model_id in prefs.items():
            if slug not in _VALID_TASK_SLUGS:
                return Response(
                    {"error": f"Invalid task: {slug}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if model_id and model_id not in allowed:
                return Response(
                    {"error": f"Model '{model_id}' not available for your tier"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Merge with existing, allowing empty string to clear
        current = tenant.task_model_preferences or {}
        for slug, model_id in prefs.items():
            if model_id:
                current[slug] = model_id
            else:
                current.pop(slug, None)

        tenant.task_model_preferences = current
        tenant.save(update_fields=["task_model_preferences"])
        tenant.bump_pending_config()
        _enqueue_immediate_apply(tenant)

        return Response({"task_model_preferences": current})


class CancelDeletionView(APIView):
    """Cancel a scheduled account deletion (only possible while subscription is active)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        tenant = getattr(user, "tenant", None)

        if not tenant or not tenant.pending_deletion:
            return Response(
                {"detail": "No scheduled deletion found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Re-activate Stripe subscription (remove cancel_at_period_end)
        if tenant.stripe_subscription_id:
            try:
                import stripe
                from django.conf import settings as dj_settings

                stripe.api_key = (
                    dj_settings.STRIPE_LIVE_SECRET_KEY
                    if getattr(dj_settings, "STRIPE_LIVE_MODE", False)
                    else dj_settings.STRIPE_TEST_SECRET_KEY
                )
                stripe.Subscription.modify(
                    tenant.stripe_subscription_id,
                    cancel_at_period_end=False,
                )
                logger.info(
                    "Reactivated subscription %s for user %s",
                    tenant.stripe_subscription_id,
                    user.id,
                )
            except Exception:
                logger.warning(
                    "Could not reactivate Stripe subscription for user %s",
                    user.id,
                    exc_info=True,
                )

        tenant.pending_deletion = False
        tenant.deletion_scheduled_at = None
        tenant.save(update_fields=["pending_deletion", "deletion_scheduled_at", "updated_at"])

        return Response({"detail": "Deletion cancelled. Your account is active."}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Entity Registry — user-facing settings UI for pii_entity_map
# ---------------------------------------------------------------------------
#
# The PII redactor mints ``[PERSON_X]`` placeholders for detected names
# and persists ``{placeholder: {name, relationship?, notes?, updated_at?}}``
# on the tenant. Two reasons users care:
#
# 1. **Wrong rehydration**: NER mis-bound a placeholder to a wrong name
#    (or a typo, or a transliteration that drifted). Without a UI, the
#    bad binding silently leaks into every future assistant reply.
# 2. **Pronoun disambiguation**: the privacy_placeholders envelope
#    section (apps/tenants/envelope.py) injects identity context into
#    the prompt — but only for entries that have user-curated
#    ``relationship`` or ``notes``. The UI is where users curate.
#
# This is a privacy surface — entries contain real names. Every endpoint
# is scoped to ``request.user.tenant``; cross-tenant access is impossible
# by construction.


class EntityRegistryListView(APIView):
    """List the tenant's pii_entity_map as registry entries.

    Returns one row per placeholder with name + metadata. Legacy
    string-shaped entries are coerced via ``apps.pii.entity_registry``
    on read so the wire format is always uniform.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.pii.entity_registry import iter_normalized

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        entries = []
        for placeholder, entry in iter_normalized(tenant.pii_entity_map):
            entries.append(
                {
                    "placeholder": placeholder,
                    "name": entry.get("name", ""),
                    "relationship": entry.get("relationship", ""),
                    "notes": entry.get("notes", ""),
                    "updated_at": entry.get("updated_at"),
                }
            )
        # Sort by placeholder for stable rendering.
        entries.sort(key=lambda e: e["placeholder"])
        return Response({"entries": entries})


class EntityRegistryItemView(APIView):
    """PATCH or DELETE a single entry by placeholder.

    PATCH body accepts ``name``, ``relationship``, ``notes`` — any
    subset. Empty strings are stored as empty (treated as "not set" by
    consumers); use DELETE to remove the entry entirely.
    """

    permission_classes = [IsAuthenticated]

    # Cap field lengths so a malicious payload can't bloat the JSONField.
    _MAX_NAME = 200
    _MAX_RELATIONSHIP = 80
    _MAX_NOTES = 500

    def patch(self, request, placeholder: str):
        from apps.pii.entity_registry import coerce, to_storage_value

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        entity_map = tenant.pii_entity_map or {}
        if placeholder not in entity_map:
            return Response(
                {"detail": f"Unknown placeholder: {placeholder}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Coerce current entry, then apply patch fields.
        current = coerce(entity_map[placeholder])
        body = request.data or {}

        # Validate types and lengths.
        def _str_field(key: str, max_len: int) -> str | None:
            if key not in body:
                return None
            value = body[key]
            if value is None:
                return ""
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            value = value.strip()
            if len(value) > max_len:
                raise ValueError(f"{key} exceeds max length {max_len}")
            return value

        try:
            patches = {
                "name": _str_field("name", self._MAX_NAME),
                "relationship": _str_field("relationship", self._MAX_RELATIONSHIP),
                "notes": _str_field("notes", self._MAX_NOTES),
            }
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Apply only fields the client sent.
        for key in ("name", "relationship", "notes"):
            if patches[key] is not None:
                current[key] = patches[key]

        # Stamp updated_at so we can detect drift / show "last edited".
        current["updated_at"] = timezone.now().isoformat()

        # Rebuild via to_storage_value to drop empty optionals + keep
        # JSON compact.
        new_value = to_storage_value(
            current.get("name", ""),
            relationship=current.get("relationship", ""),
            notes=current.get("notes", ""),
            updated_at=current["updated_at"],
        )
        entity_map[placeholder] = new_value

        Tenant.objects.filter(pk=tenant.pk).update(pii_entity_map=entity_map)
        tenant.pii_entity_map = entity_map

        return Response(
            {
                "placeholder": placeholder,
                "name": new_value.get("name", ""),
                "relationship": new_value.get("relationship", ""),
                "notes": new_value.get("notes", ""),
                "updated_at": new_value.get("updated_at"),
            }
        )

    def delete(self, request, placeholder: str):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        entity_map = tenant.pii_entity_map or {}
        if placeholder not in entity_map:
            return Response(
                {"detail": f"Unknown placeholder: {placeholder}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        del entity_map[placeholder]
        Tenant.objects.filter(pk=tenant.pk).update(pii_entity_map=entity_map)
        tenant.pii_entity_map = entity_map

        return Response(status=status.HTTP_204_NO_CONTENT)


class PIIDenylistListView(APIView):
    """List / add tenant PII denylist entries.

    The denylist is a per-tenant ``Dict[canonical_key, metadata]`` of
    words the user has marked as "not PII for me". The redactor short-
    circuits both the existing-map regex pass AND the post-NER mint
    loop for denylisted canonical keys (see ``apps/pii/redactor.py``).

    Adding an entry does NOT remove the corresponding entry from
    ``pii_entity_map`` — that's deliberate. Rehydration of stored
    placeholder refs in workspace files / chat history needs the
    entity_map entry intact; the denylist just stops it from driving
    new redaction.
    """

    permission_classes = [IsAuthenticated]
    _MAX_NAME = 200

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        denylist = tenant.pii_denylist or {}
        entries = []
        for key, meta in denylist.items():
            meta_dict = meta if isinstance(meta, dict) else {}
            entries.append(
                {
                    "key": key,
                    "reason": meta_dict.get("reason", "manual"),
                    "decided_at": meta_dict.get("decided_at"),
                }
            )
        entries.sort(key=lambda e: e["key"])
        return Response({"entries": entries})

    def post(self, request):
        from apps.pii.entity_registry import normalize_denylist_key

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        body = request.data or {}
        raw_name = body.get("name")
        if not isinstance(raw_name, str):
            return Response({"detail": "name must be a string"}, status=status.HTTP_400_BAD_REQUEST)
        name = raw_name.strip()
        if not name:
            return Response({"detail": "name is required"}, status=status.HTTP_400_BAD_REQUEST)
        if len(name) > self._MAX_NAME:
            return Response(
                {"detail": f"name exceeds max length {self._MAX_NAME}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        key = normalize_denylist_key(name)
        if not key:
            return Response(
                {"detail": "name canonicalizes to empty"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        denylist = dict(tenant.pii_denylist or {})
        denylist[key] = {
            "reason": "manual",
            "decided_at": timezone.now().isoformat(),
        }
        Tenant.objects.filter(pk=tenant.pk).update(pii_denylist=denylist)
        tenant.pii_denylist = denylist

        return Response(
            {
                "key": key,
                "reason": denylist[key]["reason"],
                "decided_at": denylist[key]["decided_at"],
            },
            status=status.HTTP_201_CREATED,
        )


class PIIDenylistItemView(APIView):
    """Remove a single denylist entry by canonical key.

    Removal re-enables redaction for the canonical key on future
    messages; existing entity_map entries with the same key resume
    driving the Step 1 regex pass.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request, key: str):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        denylist = dict(tenant.pii_denylist or {})
        if key not in denylist:
            return Response(
                {"detail": f"Unknown denylist key: {key}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        del denylist[key]
        Tenant.objects.filter(pk=tenant.pk).update(pii_denylist=denylist)
        tenant.pii_denylist = denylist
        return Response(status=status.HTTP_204_NO_CONTENT)
