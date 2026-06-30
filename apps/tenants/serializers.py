"""Tenant serializers."""

from zoneinfo import available_timezones

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import Tenant, User

_VALID_TIMEZONES = available_timezones()


def _validate_timezone(value: str) -> str:
    if value not in _VALID_TIMEZONES:
        raise serializers.ValidationError(f"Invalid timezone: {value}")
    return value


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "display_name",
            "language",
            "timezone",
            "telegram_chat_id",
            "telegram_username",
            "line_user_id",
            "line_display_name",
            "preferred_channel",
            "location_city",
            "location_lat",
            "location_lon",
        )
        read_only_fields = (
            "id",
            "username",
            "email",
            "telegram_chat_id",
            "telegram_username",
            "line_user_id",
            "line_display_name",
        )

    def validate_timezone(self, value):
        return _validate_timezone(value)

    def validate_location_lat(self, value):
        if value is not None and not (-90 <= value <= 90):
            raise serializers.ValidationError("Latitude must be between -90 and 90.")
        return value

    def validate_location_lon(self, value):
        if value is not None and not (-180 <= value <= 180):
            raise serializers.ValidationError("Longitude must be between -180 and 180.")
        return value


class TenantSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    has_active_subscription = serializers.SerializerMethodField()
    trial_days_remaining = serializers.SerializerMethodField()
    platform_budget_exceeded = serializers.SerializerMethodField()
    gravity_available = serializers.SerializerMethodField()
    effective_model = serializers.SerializerMethodField()
    free_model_offer = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = (
            "id",
            "user",
            "status",
            "model_tier",
            "has_active_subscription",
            "trial_days_remaining",
            "trial_started_at",
            "trial_ends_at",
            "is_trial",
            "container_id",
            "container_fqdn",
            "messages_today",
            "messages_this_month",
            "tokens_this_month",
            "estimated_cost_this_month",
            "monthly_token_budget",
            "monthly_cost_budget",
            "last_message_at",
            "provisioned_at",
            "config_refreshed_at",
            "config_version",
            "pending_config_version",
            "hibernated_at",
            "created_at",
            "pending_deletion",
            "deletion_scheduled_at",
            "preferred_model",
            "applied_model",
            "applied_model_at",
            "effective_model",
            "free_model_offer",
            "task_model_preferences",
            "platform_budget_exceeded",
            "finance_enabled",
            "gravity_available",
            "fuel_enabled",
            "core_enabled",
            "byo_models_enabled",
        )
        read_only_fields = fields

    def get_gravity_available(self, obj):
        """Platform-level availability of the Gravity (finance) module. When
        False (the production default — see GRAVITY_ENABLED in settings),
        Gravity is paused for privacy and the frontend hides the tab + the
        enable toggle regardless of the tenant's stored ``finance_enabled``."""
        return bool(getattr(settings, "GRAVITY_ENABLED", False))

    def get_effective_model(self, obj):
        """The model the tenant is actually on right now — the rolling free-offer
        default included. The UI uses this (not a static DEFAULT_MODEL) to render
        the Active badge when the user hasn't explicitly picked a model."""
        from apps.orchestrator.config_generator import effective_primary_model

        try:
            return effective_primary_model(obj)
        except Exception:  # noqa: BLE001 — never break the settings payload over this
            return obj.preferred_model or ""

    def get_free_model_offer(self, obj):
        """State of the limited-time free-model promotion + its health, so the
        settings page can show the banner and switch-back status."""
        from apps.billing.model_offers import offer_state

        try:
            return offer_state()
        except Exception:  # noqa: BLE001
            return {"active": False}

    def get_has_active_subscription(self, obj):
        has_real_subscription = bool(obj.stripe_subscription_id) and obj.status != Tenant.Status.DELETED
        on_trial = bool(obj.is_trial) and obj.trial_ends_at and obj.trial_ends_at > timezone.now()
        return has_real_subscription or on_trial

    def get_trial_days_remaining(self, obj):
        if not obj.is_trial or not obj.trial_ends_at:
            return None

        days = (obj.trial_ends_at - timezone.now()).days
        return max(0, days)

    def get_platform_budget_exceeded(self, obj):
        from datetime import date

        from apps.billing.models import MonthlyBudget

        first_of_month = date.today().replace(day=1)
        try:
            budget = MonthlyBudget.objects.get(month=first_of_month)
            return budget.is_over_budget
        except MonthlyBudget.DoesNotExist:
            return False


class TenantRegistrationSerializer(serializers.Serializer):
    """Used during onboarding — Telegram linking happens later via QR flow."""

    display_name = serializers.CharField(max_length=255, required=False, default="Friend")
    language = serializers.CharField(max_length=10, required=False, default="en")
    timezone = serializers.CharField(max_length=63, required=False, default="UTC")
    agent_persona = serializers.CharField(max_length=30, required=False, default="neighbor")

    def validate_timezone(self, value):
        return _validate_timezone(value)

    def validate_agent_persona(self, value):
        from apps.orchestrator.personas import PERSONAS

        if value not in PERSONAS:
            raise serializers.ValidationError(f"Unknown persona: {value}")
        return value


class HeartbeatConfigSerializer(serializers.Serializer):
    """Serializer for heartbeat window and proactive assistant settings."""

    enabled = serializers.BooleanField(required=False)
    start_hour = serializers.IntegerField(required=False, min_value=0, max_value=23)
    window_hours = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=6,
    )
    feature_tips = serializers.BooleanField(required=False)

    def validate_window_hours(self, value):
        if value > 6:
            raise serializers.ValidationError("Heartbeat window cannot exceed 6 hours.")
        return value


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = "email"

    @classmethod
    def get_token(cls, user):
        """Inject a ``pw_iat`` claim so the auth class can invalidate
        tokens issued before a password rotation. The claim is the
        user's ``password_last_changed_at`` as a unix timestamp, or 0
        when never set (legacy users — any token is accepted).

        Paired with ``JWTAuthenticationWithRLS.authenticate`` which
        rejects tokens where ``pw_iat`` is older than the current
        ``password_last_changed_at`` on the user row.
        """
        token = super().get_token(user)
        pw_changed_at = getattr(user, "password_last_changed_at", None)
        token["pw_iat"] = int(pw_changed_at.timestamp()) if pw_changed_at else 0
        return token

    def validate(self, attrs):
        # Resolve the account by EMAIL, not by the ``username`` column. The
        # stock ``authenticate(username=email)`` only matched users whose
        # ``username`` happened to equal their email, so any account created
        # with a different username — the Telegram path's ``tg_<chat_id>``, an
        # admin-made user, or anyone who later changes their email — was
        # silently locked out of web login with this exact "no active account"
        # error (indistinguishable from a wrong password). Looking up by
        # ``email__iexact`` removes that whole class of footgun and makes the
        # match case-insensitive, consistent with signup's dupe check and the
        # password-reset flow.
        email = (attrs.get("email") or "").strip()
        password = attrs.get("password") or ""
        user = User.objects.filter(email__iexact=email).first() if email else None
        # Run a hash even when the user is missing so response timing doesn't
        # reveal whether an email is registered (mirrors ModelBackend, which
        # the previous ``authenticate()`` call provided for free).
        if user is None:
            User().set_password(password)
            raise AuthenticationFailed(self.error_messages["no_active_account"], "no_active_account")
        if not user.is_active or not user.check_password(password):
            raise AuthenticationFailed(self.error_messages["no_active_account"], "no_active_account")
        self.user = user
        refresh = self.get_token(self.user)
        return {"refresh": str(refresh), "access": str(refresh.access_token)}
