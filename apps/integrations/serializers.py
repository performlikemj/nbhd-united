from rest_framework import serializers

from .models import Integration


class IntegrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Integration
        fields = ("id", "provider", "status", "provider_email", "scopes", "created_at")
        read_only_fields = ("id", "created_at")
