"""Tenant serializers."""
from rest_framework import serializers

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

    class Meta:
        model = Tenant
        fields = (
            "id", "user", "status", "model_tier",
            "container_id", "container_fqdn",
            "messages_today", "messages_this_month",
            "tokens_this_month", "estimated_cost_this_month",
            "monthly_token_budget", "last_message_at",
            "provisioned_at", "created_at",
        )
        read_only_fields = fields


class TenantRegistrationSerializer(serializers.Serializer):
    """Used during onboarding — user provides Telegram chat ID."""
    telegram_chat_id = serializers.IntegerField()
    display_name = serializers.CharField(max_length=255, required=False, default="Friend")
    language = serializers.CharField(max_length=10, required=False, default="en")
