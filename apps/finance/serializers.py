"""Finance serializers for consumer API and runtime endpoints."""

from rest_framework import serializers

from apps.pii.store_authoring import OwnerStoreSerializerMixin, author_store_fields, owner_store_representation

from .models import FinanceAccount, FinanceSnapshot, FinanceTransaction, PayoffPlan


class FinanceAccountSerializer(OwnerStoreSerializerMixin, serializers.ModelSerializer):
    pii_model_label = "finance.FinanceAccount"
    payoff_progress = serializers.FloatField(read_only=True)
    is_debt = serializers.BooleanField(read_only=True)

    class Meta:
        model = FinanceAccount
        fields = [
            "id",
            "account_type",
            "nickname",
            "current_balance",
            "original_balance",
            "interest_rate",
            "minimum_payment",
            "credit_limit",
            "due_day",
            "is_active",
            "is_debt",
            "payoff_progress",
            "pii_receipts",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "pii_receipts", "created_at", "updated_at"]

    def create(self, validated_data):
        tenant = self.context["tenant"]
        validated_data, receipts = author_store_fields(
            tenant,
            validated_data,
            model_label=self.pii_model_label,
            seam="finance.owner.account.create",
            writer="owner",
        )
        validated_data["tenant"] = tenant
        validated_data["pii_receipts"] = receipts
        # Set original_balance on first creation if not provided
        if "original_balance" not in validated_data or validated_data["original_balance"] is None:
            validated_data["original_balance"] = validated_data.get("current_balance")
        return super().create(validated_data)

    def update(self, instance, validated_data):
        authored, receipts = author_store_fields(
            instance.tenant,
            validated_data,
            model_label=self.pii_model_label,
            seam="finance.owner.account.update",
            writer="owner",
            receipts=instance.pii_receipts,
        )
        authored["pii_receipts"] = receipts
        return super().update(instance, authored)


class FinanceTransactionSerializer(OwnerStoreSerializerMixin, serializers.ModelSerializer):
    pii_model_label = "finance.FinanceTransaction"
    account_nickname = serializers.CharField(source="account.nickname", read_only=True)

    class Meta:
        model = FinanceTransaction
        fields = [
            "id",
            "account",
            "account_nickname",
            "transaction_type",
            "amount",
            "description",
            "pii_receipts",
            "date",
            "created_at",
        ]
        read_only_fields = ["id", "pii_receipts", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)

    def to_representation(self, instance):
        represented = super().to_representation(instance)
        tenant = self.context.get("tenant")
        if tenant is None or not self.context.get("rehydrate"):
            return represented

        account_data = owner_store_representation(
            instance.account,
            tenant,
            {"nickname": represented.get("account_nickname"), "pii_receipts": instance.account.pii_receipts},
            model_label="finance.FinanceAccount",
        )
        represented["account_nickname"] = account_data["nickname"]
        nickname_receipt = account_data["pii_receipts"].get("nickname")
        if nickname_receipt is not None:
            represented.setdefault("pii_receipts", {})["account_nickname"] = nickname_receipt
        return represented


class PayoffPlanSerializer(OwnerStoreSerializerMixin, serializers.ModelSerializer):
    pii_model_label = "finance.PayoffPlan"

    class Meta:
        model = PayoffPlan
        fields = [
            "id",
            "strategy",
            "monthly_budget",
            "total_debt",
            "total_interest",
            "payoff_months",
            "payoff_date",
            "schedule_json",
            "pii_receipts",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "pii_receipts", "created_at", "updated_at"]


class FinanceSnapshotSerializer(OwnerStoreSerializerMixin, serializers.ModelSerializer):
    pii_model_label = "finance.FinanceSnapshot"

    class Meta:
        model = FinanceSnapshot
        fields = [
            "id",
            "date",
            "total_debt",
            "total_savings",
            "total_payments_this_month",
            "accounts_json",
            "pii_receipts",
            "created_at",
        ]
        read_only_fields = ["id", "pii_receipts", "created_at"]


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
