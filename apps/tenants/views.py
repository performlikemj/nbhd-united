"""Tenant views."""

import logging
import re

from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.cache import tenant_cache
from apps.cron.publish import publish_task
from apps.orchestrator.config_generator import TIER_MODEL_CONFIGS

from .models import Tenant
from .serializers import HeartbeatConfigSerializer, TenantRegistrationSerializer, TenantSerializer, UserSerializer
from .services import ensure_tenant_provisioned

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

        # Create + provision the trial tenant via the shared chokepoint helper
        # (also used by the iOS web-signup PKCE handoff in ExchangeView) so the
        # two new-user paths can never drift. Idempotent; the duplicate-tenant
        # case is already handled by the 409 guard above.
        tenant, _created, provision_published = ensure_tenant_provisioned(user)
        if not provision_published:
            return Response(
                {
                    "detail": "Signup succeeded, but provisioning could not be started. Please retry shortly.",
                    "tenant_status": tenant.status,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Neighborhood invite auto-accept (the PR1.5 seam): if the signup carried
        # an invite token, resolve the edge now. Best-effort — an invalid/expired
        # token must NEVER block a signup, so failures are swallowed.
        invite_token = (serializer.validated_data.get("invite_token") or "").strip()
        if invite_token:
            self._claim_invite_quietly(tenant, user, invite_token)

        return Response(TenantSerializer(tenant).data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _claim_invite_quietly(tenant, user, token: str) -> None:
        try:
            from apps.friends.services import claim_invite

            claim_invite(tenant, user, token)
        except Exception:  # noqa: BLE001 — a bad invite never blocks signup
            logger.info("signup invite claim skipped for tenant %s (invalid/expired token)", str(tenant.id)[:8])


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


class ConstellationSettingsView(APIView):
    """PATCH: toggle constellation_enabled for the tenant.

    Constellation is a pure client-side visualization — unlike the other
    pillar toggles (Core, Finance, Fuel), there is no assistant plugin, no
    config bump, and no container restart. ``restart_required`` is always
    False; it's included only because the shared iOS pillar-toggle client
    expects the key.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        constellation_enabled = request.data.get("constellation_enabled")
        if constellation_enabled is None:
            return Response({"error": "constellation_enabled is required"}, status=status.HTTP_400_BAD_REQUEST)

        tenant.constellation_enabled = bool(constellation_enabled)
        tenant.save(update_fields=["constellation_enabled"])

        return Response(
            {
                "constellation_enabled": tenant.constellation_enabled,
                "restart_required": False,
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
            except Exception as exc:
                from apps.billing.views import _is_missing_subscription_error

                if _is_missing_subscription_error(exc):
                    # Stale subscription id from a prior Stripe account/mode: the
                    # sub doesn't exist on the live account, so there is nothing to
                    # cancel. Clear it so it stops masquerading as live linkage,
                    # then schedule deletion as normal (period_end stays None →
                    # treated as immediate-eligible by the deletion sweep).
                    Tenant.objects.filter(id=tenant.id).update(stripe_subscription_id="")
                    tenant.stripe_subscription_id = ""
                    logger.warning(
                        "Account-delete: stale stripe_subscription_id for user %s (cross-account/mode) — "
                        "cleared; scheduling deletion",
                        user.id,
                    )
                else:
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
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response(
                {"detail": "No tenant found. Complete onboarding first."},
                status=status.HTTP_404_NOT_FOUND,
            )
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
    """Return the set of model IDs allowed for this tenant's tier, plus the
    limited-time free offer (while live) and any BYO subscription extras the
    tenant has connected. Drives picker + per-task validation, so it must match
    the allowlist config_generator.resolve_tenant_models builds."""
    from apps.billing.model_offers import offer_model_entry
    from apps.orchestrator.config_generator import _byo_model_extras

    base = {**offer_model_entry(), **TIER_MODEL_CONFIGS.get(tenant.model_tier, {})}
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


class AvailableModelsView(APIView):
    """List the models this tenant may select — for the default model and the
    per-task overrides. The set is the tier base + the live free offer + any BYO
    extras (exactly what PreferredModelView/TaskModelPreferencesView validate
    against), with display names and pricing so clients never hard-code a tier's
    models. Read-only; the writes stay on the preferred-model / task-model views.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response(
                {"detail": "No tenant found. Complete onboarding first."},
                status=status.HTTP_404_NOT_FOUND,
            )

        from apps.billing.constants import MODEL_RATES, display_name_for_model

        models = []
        for model_id in _get_allowed_models(tenant):
            rate = MODEL_RATES.get(model_id, {})
            models.append(
                {
                    "id": model_id,
                    "label": display_name_for_model(model_id),
                    "input_per_million": rate.get("input"),
                    "output_per_million": rate.get("output"),
                }
            )
        models.sort(key=lambda m: m["label"].lower())

        return Response(
            {
                "models": models,
                "preferred_model": tenant.preferred_model or "",
                "model_tier": tenant.model_tier,
                "task_model_preferences": tenant.task_model_preferences or {},
                "task_slugs": sorted(_VALID_TASK_SLUGS),
            }
        )


class TaskModelPreferencesView(APIView):
    """Set per-task model overrides for scheduled jobs."""

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response(
                {"detail": "No tenant found. Complete onboarding first."},
                status=status.HTTP_404_NOT_FOUND,
            )
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
            except Exception as exc:
                from apps.billing.views import _is_missing_subscription_error

                if _is_missing_subscription_error(exc):
                    # Stale subscription id from a prior Stripe account/mode — the
                    # sub no longer exists, so there is nothing to reactivate.
                    # Clear it; the user can re-subscribe to relink to the live
                    # account. Cancellation is still removed DB-side below.
                    Tenant.objects.filter(id=tenant.id).update(stripe_subscription_id="")
                    tenant.stripe_subscription_id = ""
                    logger.warning(
                        "Cancel-deletion: stale stripe_subscription_id for user %s (cross-account/mode) — cleared",
                        user.id,
                    )
                else:
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
    """List (GET) the tenant's pii_entity_map, or manually add (POST) a binding.

    GET returns one row per placeholder with name + metadata. Legacy
    string-shaped entries are coerced via ``apps.pii.entity_registry``
    on read so the wire format is always uniform.

    POST is the "hide this too" front door: the user names a person/place the
    detector never caught (or that they proactively want obfuscated) and it is
    minted into the SAME map the redactor drives — so from the next inbound
    message on it redacts, rehydrates, shows in chips, and appears in the review
    queue exactly like any detector-minted binding. See ``post`` for the wire
    contract.
    """

    permission_classes = [IsAuthenticated]

    # Field caps. Name follows the add contract (1..256); relationship/notes
    # mirror EntityRegistryItemView so the two write paths stay consistent.
    _MAX_NAME = 256
    _MAX_RELATIONSHIP = 80
    _MAX_NOTES = 500

    # Manual adds only ever obfuscate the two free-text contextual classes — the
    # structured/secret types come from checksum recognizers, not user typing.
    _ALLOWED_TYPES = frozenset({"PERSON", "LOCATION"})

    # Friendly, class-naming copy for the footgun (is_junk_span) 422. Keyed by
    # the hygiene reason code; the fallback covers the "common word / fragment"
    # framing the UI confirm ("Hide it anyway?") is written against.
    _DEFAULT_JUNK_WARNING = (
        "That looks like a common word or fragment — hiding it may make conversations with your assistant confusing."
    )
    _JUNK_WARNINGS = {
        "too_short": (
            "That's very short — hiding a single letter or symbol may make conversations with your assistant confusing."
        ),
        "structure": (
            "That looks like formatting or machine text rather than a name — "
            "hiding it may make conversations with your assistant confusing."
        ),
        "invisible": (
            "That contains hidden or invisible characters rather than a plain "
            "name — hiding it may make conversations with your assistant confusing."
        ),
        "placeholder_fragment": (
            "That looks like a redaction placeholder, not a real name — hiding "
            "it may make conversations with your assistant confusing."
        ),
        "numeric_datelike": (
            "That looks like a number, measurement, or date rather than a name — "
            "hiding it may make conversations with your assistant confusing."
        ),
        "identifier": (
            "That looks like a filename or code identifier rather than a name — "
            "hiding it may make conversations with your assistant confusing."
        ),
    }

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

    def post(self, request):
        """Manually mint (or merge onto) an entity-registry binding.

        Body: ``{"name": <required 1..256>, "entity_type": "PERSON"|"LOCATION"
        (default PERSON), "relationship"?, "notes"?, "acknowledge_warning"?}``.

        Responses:
        - 201 — a NEW binding was minted. ``{"placeholder", "name",
          "relationship", "notes", "denylist_removed"}``.
        - 200 — the name was ALREADY bound (case-insensitive canonical match):
          the existing placeholder is returned and relationship/notes are merged
          onto it when provided. Same body shape.
        - 422 — the hygiene heuristics flag the name as a probable common-word /
          fragment footgun AND ``acknowledge_warning`` was not true. ``{"warning":
          <sentence>}``. Clients confirm ("Hide it anyway?") and retry with
          ``acknowledge_warning=true``.
        - 400 — validation (missing/empty/too-long name, bad entity_type).

        Minting uses the SAME per-type monotonic high-water counters as the
        redactor (``Tenant.pii_type_counters`` via
        ``redactor.next_placeholder_number``) so numbers are never reused; adding
        a name REMOVES its canonical key from ``pii_denylist`` (newest user intent
        wins). The binding then behaves like any detector-minted one on every
        channel.
        """
        from apps.pii.entity_registry import (
            canonical_key,
            coerce,
            inverted_names_ci,
            normalize_denylist_key,
            to_storage_value,
        )
        from apps.pii.hygiene import is_junk_span
        from apps.pii.redactor import next_placeholder_number

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        body = request.data or {}

        # --- name: required, stripped, 1..256 -----------------------------------
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

        # --- entity_type: optional, default PERSON, {PERSON, LOCATION} ----------
        raw_etype = body.get("entity_type", "PERSON")
        if raw_etype is None:
            raw_etype = "PERSON"
        if not isinstance(raw_etype, str):
            return Response({"detail": "entity_type must be a string"}, status=status.HTTP_400_BAD_REQUEST)
        etype = raw_etype.strip().upper()
        if etype not in self._ALLOWED_TYPES:
            return Response(
                {"detail": f"entity_type must be one of {sorted(self._ALLOWED_TYPES)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- relationship / notes: optional strings (None => not provided) ------
        def _opt_str(key: str, max_len: int) -> str | None:
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
            relationship = _opt_str("relationship", self._MAX_RELATIONSHIP)
            notes = _opt_str("notes", self._MAX_NOTES)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        acknowledge_warning = bool(body.get("acknowledge_warning", False))

        # --- Footgun screen BEFORE mutating -------------------------------------
        # The redactor uses is_junk_span to keep common-word/fragment junk from
        # ever minting; a manual add that trips the same heuristics is almost
        # always a mistake (hiding "the", a date, a filename) that would make the
        # assistant's replies confusing. Surface it as a 422 the client confirms;
        # acknowledge_warning=true is the explicit "hide it anyway" bypass.
        junk, reason = is_junk_span(name, etype)
        if junk and not acknowledge_warning:
            return Response(
                {"warning": self._JUNK_WARNINGS.get(reason, self._DEFAULT_JUNK_WARNING)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Serialize the read-modify-write per tenant: the inbound redactor mints
        # by overwriting the whole pii_entity_map + pii_type_counters under this
        # same row lock. Re-read the locked snapshot so a manual add and a
        # concurrent detector mint can never clobber each other or recycle a
        # number. All touched fields commit in ONE update().
        with transaction.atomic():
            locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
            entity_map = dict((locked.pii_entity_map if locked else None) or {})
            denylist = dict((locked.pii_denylist if locked else None) or {})
            stored_counters = dict((locked.pii_type_counters if locked else None) or {})

            now = timezone.now().isoformat()

            # Newest user intent wins: naming something to hide clears any deny
            # key that would otherwise stop it from redacting.
            deny_key = normalize_denylist_key(name)
            denylist_removed = bool(deny_key and deny_key in denylist)
            if denylist_removed:
                del denylist[deny_key]

            existing = inverted_names_ci(entity_map).get(canonical_key(name))
            if existing is not None:
                # Already bound (case-insensitive) — return the existing
                # placeholder and merge provided relationship/notes onto it.
                _display, placeholder = existing
                current = coerce(entity_map[placeholder])
                if relationship is not None:
                    current["relationship"] = relationship
                if notes is not None:
                    current["notes"] = notes
                new_value = to_storage_value(
                    current.get("name", ""),
                    relationship=current.get("relationship", ""),
                    notes=current.get("notes", ""),
                    updated_at=now,
                    arbiter_judged_at=current.get("arbiter_judged_at"),
                    reviewed_at=current.get("reviewed_at"),
                )
                entity_map[placeholder] = new_value
                response_status = status.HTTP_200_OK
                update_fields: dict = {"pii_entity_map": entity_map}
            else:
                # Mint a fresh binding using the SAME monotonic high-water the
                # redactor uses, then advance the stored counter in the same
                # write so the number can never be reissued.
                number = next_placeholder_number(etype, entity_map, stored_counters)
                placeholder = f"[{etype}_{number}]"
                stored_counters[etype] = number
                new_value = to_storage_value(
                    name,
                    relationship=relationship or "",
                    notes=notes or "",
                    updated_at=now,
                )
                entity_map[placeholder] = new_value
                response_status = status.HTTP_201_CREATED
                update_fields = {
                    "pii_entity_map": entity_map,
                    "pii_type_counters": stored_counters,
                }

            if denylist_removed:
                update_fields["pii_denylist"] = denylist

            Tenant.objects.filter(pk=tenant.pk).update(**update_fields)

        tenant.pii_entity_map = entity_map
        tenant.pii_type_counters = stored_counters
        tenant.pii_denylist = denylist

        return Response(
            {
                "placeholder": placeholder,
                "name": new_value.get("name", ""),
                "relationship": new_value.get("relationship", ""),
                "notes": new_value.get("notes", ""),
                "denylist_removed": denylist_removed,
            },
            status=response_status,
        )


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

        body = request.data or {}

        # Validate types and lengths (no DB; keep this out of the lock).
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

        # Serialize the read-modify-write per tenant: the inbound redactor,
        # the arbiter sweep, and concurrent edits all overwrite the whole
        # pii_entity_map dict. Re-read the row under a lock so this edit can't
        # be clobbered by — or clobber — a concurrent mint/delete.
        with transaction.atomic():
            locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
            entity_map = dict((locked.pii_entity_map if locked else None) or {})
            if placeholder not in entity_map:
                return Response(
                    {"detail": f"Unknown placeholder: {placeholder}"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Coerce current entry, then apply patch fields.
            current = coerce(entity_map[placeholder])

            # Apply only fields the client sent.
            for key in ("name", "relationship", "notes"):
                if patches[key] is not None:
                    current[key] = patches[key]

            # Stamp updated_at so we can detect drift / show "last edited".
            current["updated_at"] = timezone.now().isoformat()

            # Rebuild via to_storage_value to drop empty optionals + keep
            # JSON compact. Preserve arbiter_judged_at so the next arbiter
            # sweep does not re-evaluate an already-judged entry just because
            # the user edited its name/relationship/notes, and reviewed_at so a
            # kept entry does not bounce back into the review queue on edit.
            new_value = to_storage_value(
                current.get("name", ""),
                relationship=current.get("relationship", ""),
                notes=current.get("notes", ""),
                updated_at=current["updated_at"],
                arbiter_judged_at=current.get("arbiter_judged_at"),
                reviewed_at=current.get("reviewed_at"),
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

        # Serialize the read-modify-write per tenant so a concurrent redactor
        # mint (full-dict overwrite) cannot resurrect the deleted entry.
        with transaction.atomic():
            locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
            entity_map = dict((locked.pii_entity_map if locked else None) or {})
            if placeholder not in entity_map:
                return Response(
                    {"detail": f"Unknown placeholder: {placeholder}"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            del entity_map[placeholder]
            Tenant.objects.filter(pk=tenant.pk).update(pii_entity_map=entity_map)
        tenant.pii_entity_map = entity_map

        return Response(status=status.HTTP_204_NO_CONTENT)


class EntityRegistryBulkDeleteView(APIView):
    """Bulk-delete entity-registry bindings in a single request.

    Backs the People settings page's "Delete N selected" action. Deleting
    hundreds of bindings via sequential single-entry DELETEs is slow and
    racy against the inbound redactor's full-dict overwrites; this drains
    them under one row lock in a single round trip.

    Body: ``{"placeholders": ["[PERSON_1]", ...], "deny": false}``.

    When ``deny`` is true, each deleted entry's name is ALSO added to the
    tenant's pii_denylist. Deletion alone does NOT stop future redaction —
    the redactor's NER pass re-mints a fresh ``[TYPE_N]`` for the same real
    name on the next inbound message unless the value is on the denylist
    (see apps/pii/redactor.py and the PIIDenylistListView docstring). So
    ``deny=true`` is the "actually stop obfuscating this value" lever;
    deleting the row without denying just re-mints the value under a new
    placeholder and breaks rehydration of any stored text still referencing
    the old one.
    """

    permission_classes = [IsAuthenticated]
    _MAX_BATCH = 1000

    def post(self, request):
        from apps.pii.entity_registry import get_name, normalize_denylist_key

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        body = request.data or {}
        placeholders = body.get("placeholders")
        if not isinstance(placeholders, list):
            return Response(
                {"detail": "placeholders must be a list"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not placeholders:
            return Response(
                {"detail": "placeholders is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(placeholders) > self._MAX_BATCH:
            return Response(
                {"detail": f"batch exceeds max size {self._MAX_BATCH}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not all(isinstance(p, str) for p in placeholders):
            return Response(
                {"detail": "placeholders must be a list of strings"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deny = bool(body.get("deny", False))

        deleted: list[str] = []
        not_found: list[str] = []
        denied: list[str] = []

        # Serialize the read-modify-write per tenant: the inbound redactor,
        # the arbiter sweep, and concurrent edits all overwrite the whole
        # pii_entity_map / pii_denylist dicts. Re-read both rows under one
        # lock so this batch can't be clobbered by — or clobber — a
        # concurrent mint/delete, and write both fields in a single UPDATE
        # so the delete and the deny commit atomically.
        with transaction.atomic():
            locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
            entity_map = dict((locked.pii_entity_map if locked else None) or {})
            denylist = dict((locked.pii_denylist if locked else None) or {})

            now = timezone.now().isoformat()
            for placeholder in placeholders:
                if placeholder not in entity_map:
                    not_found.append(placeholder)
                    continue
                entry = entity_map.pop(placeholder)
                deleted.append(placeholder)
                if deny:
                    # get_name reads both the dict and legacy bare-string
                    # entry shapes; normalize_denylist_key casefold+strips
                    # to the canonical denylist key. Skip empties and keep
                    # existing denylist entries untouched (only genuinely
                    # new keys are added and reported in ``denied``).
                    key = normalize_denylist_key(get_name(entry))
                    if key and key not in denylist:
                        denylist[key] = {"reason": "bulk-delete", "decided_at": now}
                        denied.append(key)

            update_fields = {"pii_entity_map": entity_map}
            if deny:
                update_fields["pii_denylist"] = denylist
            Tenant.objects.filter(pk=tenant.pk).update(**update_fields)
        tenant.pii_entity_map = entity_map
        if deny:
            tenant.pii_denylist = denylist

        return Response(
            {"deleted": deleted, "not_found": not_found, "denied": denied},
            status=status.HTTP_200_OK,
        )


# Placeholder-number parser for review-queue ordering. Mirrors
# apps.pii.entity_registry._PLACEHOLDER_NUM_RE but is kept local so the view
# layer doesn't reach into that module's private surface. Malformed
# placeholders sort to 0 (oldest) and lose the "newest first" ordering tie.
_REVIEW_PLACEHOLDER_NUM_RE = re.compile(r"\[[A-Z_]+_(\d+)\]")

# Placeholders the review queue surfaces. The tier-2 flow only asks the user to
# judge free-text PERSON / LOCATION spans — the neural-NER classes with the
# junk long tail (see the cleanup audit). Pattern-validated classes
# (EMAIL_ADDRESS, CREDIT_CARD, IBAN) are high-precision and never queued.
_REVIEW_PREFIXES = ("[PERSON_", "[LOCATION_")
_REVIEW_QUEUE_CAP = 200


def _review_placeholder_num(placeholder: str) -> int:
    match = _REVIEW_PLACEHOLDER_NUM_RE.match(placeholder)
    return int(match.group(1)) if match else 0


class PIIReviewQueueView(APIView):
    """Tier-2 review surface — "here is what your assistant is hiding".

    Returns the PERSON_*/LOCATION_* bindings the user has not yet reviewed
    (no ``reviewed_at`` stamp). The audit that motivated this found ~89% of a
    heavy tenant's bindings were junk minted from agent-authored notes, tool
    responses, and unvalidated neural labels; the retired cloud arbiter used to
    triage that. This queue replaces the cloud egress with a local, user-driven
    (or on-device-model) keep/clean pass.

    This is ALSO the contract the iOS on-device-model flow consumes: GET the
    queue, judge each span locally, then POST keep (this view's sibling) for the
    real bindings and the existing bulk-delete (``deny=true``) for the junk.

    ``entries`` is capped at the newest ``_REVIEW_QUEUE_CAP`` placeholders
    (highest number first); ``total`` is the full unreviewed count so the UI can
    say "hiding N values" even when more than one page is outstanding.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.pii.entity_registry import coerce

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        unreviewed: list[dict] = []
        for placeholder, raw in (tenant.pii_entity_map or {}).items():
            if not placeholder.startswith(_REVIEW_PREFIXES):
                continue
            entry = coerce(raw)
            if entry.get("reviewed_at"):
                continue
            unreviewed.append(
                {
                    "placeholder": placeholder,
                    "name": entry.get("name", ""),
                    "relationship": entry.get("relationship", ""),
                    "notes": entry.get("notes", ""),
                }
            )

        total = len(unreviewed)
        # Newest first — the freshest mints are the ones most likely to be the
        # junk the user wants to catch before it accretes.
        unreviewed.sort(key=lambda e: _review_placeholder_num(e["placeholder"]), reverse=True)
        return Response({"entries": unreviewed[:_REVIEW_QUEUE_CAP], "total": total})


class PIIReviewQueueKeepView(APIView):
    """Mark PERSON/LOCATION bindings as reviewed-and-kept.

    Body: ``{"placeholders": ["[PERSON_1]", ...]}``. Stamps ``reviewed_at``
    (iso now) on each existing entry so it drops out of the review queue.
    Unknown placeholders come back in ``not_found``; this is the "keep" verdict.
    The "clean" verdict has no endpoint of its own — clients call the existing
    bulk-delete with ``deny=true`` so the junk value is both removed and stopped
    from being re-minted.
    """

    permission_classes = [IsAuthenticated]
    _MAX_BATCH = 1000

    def post(self, request):
        from apps.pii.entity_registry import coerce, to_storage_value

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        body = request.data or {}
        placeholders = body.get("placeholders")
        if not isinstance(placeholders, list):
            return Response(
                {"detail": "placeholders must be a list"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not placeholders:
            return Response(
                {"detail": "placeholders is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(placeholders) > self._MAX_BATCH:
            return Response(
                {"detail": f"batch exceeds max size {self._MAX_BATCH}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not all(isinstance(p, str) for p in placeholders):
            return Response(
                {"detail": "placeholders must be a list of strings"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        kept: list[str] = []
        not_found: list[str] = []

        # Serialize the read-modify-write per tenant: the inbound redactor and
        # the (retired) arbiter sweep overwrite the whole pii_entity_map dict.
        # Re-read the row under a lock so stamping reviewed_at can't be
        # clobbered by — or clobber — a concurrent mint/delete.
        with transaction.atomic():
            locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
            entity_map = dict((locked.pii_entity_map if locked else None) or {})

            now = timezone.now().isoformat()
            for placeholder in placeholders:
                if placeholder not in entity_map:
                    not_found.append(placeholder)
                    continue
                current = coerce(entity_map[placeholder])
                # Rebuild via to_storage_value to keep the entry compact and
                # preserve every stamp (updated_at, arbiter_judged_at) alongside
                # the new reviewed_at, so keeping never drops identity metadata.
                entity_map[placeholder] = to_storage_value(
                    current.get("name", ""),
                    relationship=current.get("relationship", ""),
                    notes=current.get("notes", ""),
                    updated_at=current.get("updated_at"),
                    arbiter_judged_at=current.get("arbiter_judged_at"),
                    reviewed_at=now,
                )
                kept.append(placeholder)

            Tenant.objects.filter(pk=tenant.pk).update(pii_entity_map=entity_map)
        tenant.pii_entity_map = entity_map

        return Response({"kept": kept, "not_found": not_found}, status=status.HTTP_200_OK)


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

        # Serialize per tenant: the arbiter sweep also writes the full
        # pii_denylist dict, so an unlocked overwrite here could drop an
        # arbiter-added key (or vice-versa). Re-read under a row lock.
        with transaction.atomic():
            locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
            denylist = dict((locked.pii_denylist if locked else None) or {})
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

        # Serialize per tenant so a concurrent arbiter/manual denylist write
        # (full-dict overwrite) cannot resurrect the key we delete here.
        with transaction.atomic():
            locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
            denylist = dict((locked.pii_denylist if locked else None) or {})
            if key not in denylist:
                return Response(
                    {"detail": f"Unknown denylist key: {key}"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            del denylist[key]
            Tenant.objects.filter(pk=tenant.pk).update(pii_denylist=denylist)
        tenant.pii_denylist = denylist
        return Response(status=status.HTTP_204_NO_CONTENT)


class PIIDenylistBulkView(APIView):
    """Bulk-add names to the tenant denylist in a single request.

    Designed for the People settings page's "Ignore N selected" action
    over the 826-row canary case — sequential single-entry POSTs would
    take ~40s to drain. One round trip drains in ~200ms.

    Bad individual entries (empty after strip, too long, canonicalizes
    to empty) are skipped, not fatal: the response lists ``added`` keys
    and ``skipped`` items with reasons so the UI can show partial
    success. Total batch size is capped to prevent runaway payloads.
    """

    permission_classes = [IsAuthenticated]
    _MAX_NAME = 200
    _MAX_BATCH = 1000

    def post(self, request):
        from apps.pii.entity_registry import normalize_denylist_key

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        body = request.data or {}
        names = body.get("names")
        if not isinstance(names, list):
            return Response(
                {"detail": "names must be a list"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(names) > self._MAX_BATCH:
            return Response(
                {"detail": f"batch exceeds max size {self._MAX_BATCH}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now().isoformat()
        added: list[str] = []
        skipped: list[dict] = []

        # Validate + canonicalize outside the lock (no DB).
        new_entries: dict[str, dict] = {}
        for raw in names:
            if not isinstance(raw, str):
                skipped.append({"name": str(raw)[:64], "reason": "not a string"})
                continue
            name = raw.strip()
            if not name:
                skipped.append({"name": raw[:64], "reason": "empty"})
                continue
            if len(name) > self._MAX_NAME:
                skipped.append({"name": name[:64], "reason": f"exceeds max length {self._MAX_NAME}"})
                continue
            key = normalize_denylist_key(name)
            if not key:
                skipped.append({"name": name[:64], "reason": "canonicalizes to empty"})
                continue
            new_entries[key] = {"reason": "manual", "decided_at": now}
            added.append(key)

        # Serialize the read-modify-write per tenant: the arbiter sweep and the
        # single-entry endpoints also overwrite the whole pii_denylist dict, so
        # an unlocked merge here could drop their concurrent writes. Re-read
        # under a row lock and merge new entries on top.
        with transaction.atomic():
            locked = Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
            denylist = dict((locked.pii_denylist if locked else None) or {})
            denylist.update(new_entries)
            Tenant.objects.filter(pk=tenant.pk).update(pii_denylist=denylist)
        tenant.pii_denylist = denylist

        return Response(
            {"added": added, "skipped": skipped},
            status=status.HTTP_200_OK,
        )
