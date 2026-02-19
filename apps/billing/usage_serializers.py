"""Serializers for usage dashboard endpoints."""
from rest_framework import serializers


class ModelBreakdownSerializer(serializers.Serializer):
    model = serializers.CharField()
    display_name = serializers.CharField()
    input_tokens = serializers.IntegerField()
    output_tokens = serializers.IntegerField()
    cost = serializers.FloatField()
    count = serializers.IntegerField()


class BudgetSerializer(serializers.Serializer):
    tenant_tokens_used = serializers.IntegerField()
    tenant_token_budget = serializers.IntegerField()
    tenant_estimated_cost = serializers.FloatField()
    budget_percentage = serializers.FloatField()
    global_spent = serializers.FloatField()
    global_remaining = serializers.FloatField(allow_null=True)


class PeriodSerializer(serializers.Serializer):
    start = serializers.CharField()
    end = serializers.CharField()


class UsageSummarySerializer(serializers.Serializer):
    period = PeriodSerializer()
    total_input_tokens = serializers.IntegerField()
    total_output_tokens = serializers.IntegerField()
    total_tokens = serializers.IntegerField()
    total_cost = serializers.FloatField()
    message_count = serializers.IntegerField()
    by_model = ModelBreakdownSerializer(many=True)
    budget = BudgetSerializer()


class DailyUsageSerializer(serializers.Serializer):
    date = serializers.CharField()
    input_tokens = serializers.IntegerField()
    output_tokens = serializers.IntegerField()
    cost = serializers.FloatField()
    message_count = serializers.IntegerField()


class ModelRateSerializer(serializers.Serializer):
    model = serializers.CharField()
    display_name = serializers.CharField()
    input_per_million = serializers.FloatField()
    output_per_million = serializers.FloatField()


class InfraBreakdownSerializer(serializers.Serializer):
    container = serializers.FloatField()
    database_share = serializers.FloatField()
    storage_share = serializers.FloatField()
    total = serializers.FloatField()


class TransparencySerializer(serializers.Serializer):
    period = PeriodSerializer()
    subscription_price = serializers.FloatField()
    your_actual_cost = serializers.FloatField()
    platform_margin = serializers.FloatField()
    margin_percentage = serializers.FloatField()
    target_margin_percentage = serializers.FloatField()
    message_count = serializers.IntegerField()
    model_rates = ModelRateSerializer(many=True)
    infra_breakdown = InfraBreakdownSerializer()
    explanation = serializers.CharField()
