"""Tenant serializers."""
from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import Tenant, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id", "username", "email", "display_name", "language",
            "telegram_chat_id", "telegram_username",
        )
        read_only_fields = ("id",)


class TenantSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    has_active_subscription = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = (
            "id", "user", "status", "model_tier",
            "has_active_subscription",
            "container_id", "container_fqdn",
            "messages_today", "messages_this_month",
            "tokens_this_month", "estimated_cost_this_month",
            "monthly_token_budget", "last_message_at",
            "provisioned_at", "created_at",
        )
        read_only_fields = fields

    def get_has_active_subscription(self, obj):
        return bool(obj.stripe_subscription_id) and obj.status != Tenant.Status.DELETED


class TenantRegistrationSerializer(serializers.Serializer):
    """Used during onboarding — Telegram linking happens later via QR flow."""
    display_name = serializers.CharField(max_length=255, required=False, default="Friend")
    language = serializers.CharField(max_length=10, required=False, default="en")
    agent_persona = serializers.CharField(max_length=30, required=False, default="neighbor")

    def validate_agent_persona(self, value):
        from apps.orchestrator.personas import PERSONAS
        if value not in PERSONAS:
            raise serializers.ValidationError(f"Unknown persona: {value}")
        return value


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = "email"

    def validate(self, attrs):
        self.user = authenticate(
            request=self.context.get("request"),
            username=attrs.get("email", ""),
            password=attrs.get("password", ""),
        )
        if self.user is None or not self.user.is_active:
            raise AuthenticationFailed(
                self.error_messages["no_active_account"], "no_active_account",
            )
        refresh = self.get_token(self.user)
        return {"refresh": str(refresh), "access": str(refresh.access_token)}
