"""DRF serializers for the Neighborhood console."""

from __future__ import annotations

from rest_framework import serializers

from . import services
from .models import NeighborProfile


class NeighborProfileSerializer(serializers.ModelSerializer):
    """Read + partial-update of the caller's own profile. ``handle`` is
    validated/normalized through :func:`services.validate_handle` (format,
    reserved list, cross-tenant uniqueness)."""

    class Meta:
        model = NeighborProfile
        fields = ("handle", "display_name", "bio", "avatar_hue")

    def validate_handle(self, value):
        tenant = self.context["tenant"]
        return services.validate_handle(value, tenant)

    def validate_avatar_hue(self, value):
        if value is None or not (0 <= int(value) <= 359):
            raise serializers.ValidationError("Hue must be between 0 and 359.")
        return int(value)


class WaveCreateSerializer(serializers.Serializer):
    handle = serializers.CharField(max_length=30)
    note = serializers.CharField(max_length=280, required=False, allow_blank=True, default="")


class InviteCreateSerializer(serializers.Serializer):
    max_uses = serializers.IntegerField(required=False, min_value=1, max_value=services.MAX_INVITE_USES, default=1)
    expires_in_days = serializers.IntegerField(
        required=False, min_value=1, max_value=services.MAX_INVITE_DAYS, default=services.DEFAULT_INVITE_DAYS
    )
