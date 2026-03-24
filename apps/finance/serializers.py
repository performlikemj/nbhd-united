"""Finance serializers for consumer API and runtime endpoints."""
from rest_framework import serializers

from .models import FinanceAccount, FinanceSnapshot, FinanceTransaction, PayoffPlan


class FinanceAccountSerializer(serializers.ModelSerializer):
    payoff_progress = serializers.FloatField(read_only=True)
    is_debt = serializers.BooleanField(read_only=True)

    class Meta:
        model = FinanceAccount
        fields = [
            "id", "account_type", "nickname", "current_balance",
            "original_balance", "interest_rate", "minimum_payment",
            "credit_limit", "due_day", "is_active", "is_debt",
            "payoff_progress", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        # Set original_balance on first creation if not provided
        if "original_balance" not in validated_data or validated_data["original_balance"] is None:
            validated_data["original_balance"] = validated_data.get("current_balance")
        return super().create(validated_data)


class FinanceTransactionSerializer(serializers.ModelSerializer):
    account_nickname = serializers.CharField(source="account.nickname", read_only=True)

    class Meta:
        model = FinanceTransaction
        fields = [
            "id", "account", "account_nickname", "transaction_type",
            "amount", "description", "date", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class PayoffPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayoffPlan
        fields = [
            "id", "strategy", "monthly_budget", "total_debt",
            "total_interest", "payoff_months", "payoff_date",
            "schedule_json", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class FinanceSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinanceSnapshot
        fields = [
            "id", "date", "total_debt", "total_savings",
            "total_payments_this_month", "accounts_json", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class FinanceDashboardSerializer(serializers.Serializer):
    total_debt = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_savings = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_minimum_payments = serializers.DecimalField(max_digits=12, decimal_places=2)
    debt_account_count = serializers.IntegerField()
    savings_account_count = serializers.IntegerField()
    accounts = FinanceAccountSerializer(many=True)
    active_plan = PayoffPlanSerializer(allow_null=True)
    snapshots = FinanceSnapshotSerializer(many=True)
    recent_transactions = FinanceTransactionSerializer(many=True)
