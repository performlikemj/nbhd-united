"""Serializers for automations and automation run history."""
from __future__ import annotations

from rest_framework import serializers

from .models import Automation, AutomationRun
from .services import (
    AutomationLimitError,
    AutomationValidationError,
    create_automation,
    update_automation,
    validate_schedule,
    validate_timezone_name,
)


class AutomationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Automation
        fields = (
            "id",
            "kind",
            "status",
            "timezone",
            "schedule_type",
            "schedule_time",
            "schedule_days",
            "quiet_hours_start",
            "quiet_hours_end",
            "last_run_at",
            "next_run_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "last_run_at",
            "next_run_at",
            "created_at",
            "updated_at",
        )

    def validate_timezone(self, value: str) -> str:
        try:
            validate_timezone_name(value)
        except AutomationValidationError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def validate(self, attrs: dict) -> dict:
        instance = getattr(self, "instance", None)

        schedule_type = attrs.get("schedule_type", getattr(instance, "schedule_type", None))
        if schedule_type is None:
            raise serializers.ValidationError({"schedule_type": "This field is required."})

        schedule_days = attrs.get("schedule_days", getattr(instance, "schedule_days", None))
        try:
            normalized_days = validate_schedule(schedule_type, schedule_days)
        except AutomationValidationError as exc:
            raise serializers.ValidationError({"schedule_days": str(exc)}) from exc
        if instance is None or "schedule_days" in attrs or "schedule_type" in attrs:
            attrs["schedule_days"] = normalized_days

        timezone_name = attrs.get("timezone", getattr(instance, "timezone", None))
        if timezone_name is None:
            raise serializers.ValidationError({"timezone": "This field is required."})

        try:
            validate_timezone_name(timezone_name)
        except AutomationValidationError as exc:
            raise serializers.ValidationError({"timezone": str(exc)}) from exc

        return attrs

    def create(self, validated_data: dict) -> Automation:
        tenant = self.context["tenant"]
        try:
            return create_automation(tenant=tenant, validated_data=validated_data)
        except (AutomationValidationError, AutomationLimitError) as exc:
            raise serializers.ValidationError({"detail": str(exc)}) from exc

    def update(self, instance: Automation, validated_data: dict) -> Automation:
        try:
            return update_automation(automation=instance, validated_data=validated_data)
        except (AutomationValidationError, AutomationLimitError) as exc:
            raise serializers.ValidationError({"detail": str(exc)}) from exc


class AutomationRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = AutomationRun
        fields = (
            "id",
            "automation",
            "tenant",
            "status",
            "trigger_source",
            "scheduled_for",
            "started_at",
            "finished_at",
            "idempotency_key",
            "input_payload",
            "result_payload",
            "error_message",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields
