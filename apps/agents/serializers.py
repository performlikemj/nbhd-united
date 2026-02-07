from rest_framework import serializers

from .models import AgentSession, MemoryItem, Message


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ("id", "session", "role", "content", "metadata", "tokens_used", "created_at")
        read_only_fields = ("id", "tokens_used", "created_at")


class AgentSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentSession
        fields = ("id", "title", "is_active", "message_count", "created_at", "updated_at")
        read_only_fields = ("id", "message_count", "created_at", "updated_at")


class MemoryItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = MemoryItem
        fields = ("id", "key", "value", "category", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")
