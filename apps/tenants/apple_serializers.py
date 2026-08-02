"""Strict request serializers for Sign in with Apple endpoints."""

from __future__ import annotations

from collections.abc import Mapping

from rest_framework import serializers


class StrictCharField(serializers.CharField):
    """A CharField that rejects DRF's usual number-to-string coercion."""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        try:
            data.encode("utf-8")
        except UnicodeEncodeError:
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictUUIDField(serializers.UUIDField):
    """A UUIDField that accepts only the contract's JSON string shape."""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictRequestSerializer(serializers.Serializer):
    """Reject unknown fields instead of silently ignoring them."""

    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            raise serializers.ValidationError("Expected an object.")
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError("Unknown field.")
        return super().to_internal_value(data)


class AppleBeginSerializer(StrictRequestSerializer):
    purpose = serializers.ChoiceField(
        choices=("web_auth", "native_auth"),
        default="web_auth",
        required=False,
    )


class AppleCompleteSerializer(StrictRequestSerializer):
    transaction_id = StrictUUIDField()
    code = StrictCharField(max_length=1024, trim_whitespace=False)
    state = StrictCharField(max_length=128, trim_whitespace=False)


class AppleNativeSerializer(StrictRequestSerializer):
    transaction_id = StrictUUIDField()
    identity_token = StrictCharField(max_length=4096, trim_whitespace=False)
    state = StrictCharField(max_length=128, trim_whitespace=False)


class AppleLinkSerializer(AppleCompleteSerializer):
    current_password = StrictCharField(max_length=128, trim_whitespace=False)
