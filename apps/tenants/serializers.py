from rest_framework import serializers

from .models import AgentConfig, Tenant, User


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = ("id", "name", "slug", "plan_tier", "is_active", "created_at")
        read_only_fields = ("id", "created_at")


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "display_name", "language", "tenant", "telegram_chat_id")
        read_only_fields = ("id", "tenant")


class AgentConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentConfig
        fields = ("id", "name", "system_prompt", "model_tier", "temperature", "max_tokens_per_message")
        read_only_fields = ("id",)
