from django.contrib import admin

from .models import FinanceAccount, FinanceSnapshot, FinanceTransaction, PayoffPlan


@admin.register(FinanceAccount)
class FinanceAccountAdmin(admin.ModelAdmin):
    list_display = ["nickname", "tenant", "account_type", "current_balance", "interest_rate", "is_active"]
    list_filter = ["account_type", "is_active"]
    search_fields = ["nickname", "tenant__user__display_name"]


@admin.register(FinanceTransaction)
class FinanceTransactionAdmin(admin.ModelAdmin):
    list_display = ["account", "transaction_type", "amount", "date"]
    list_filter = ["transaction_type"]


@admin.register(PayoffPlan)
class PayoffPlanAdmin(admin.ModelAdmin):
    list_display = ["tenant", "strategy", "payoff_months", "total_interest", "is_active"]
    list_filter = ["strategy", "is_active"]


@admin.register(FinanceSnapshot)
class FinanceSnapshotAdmin(admin.ModelAdmin):
    list_display = ["tenant", "date", "total_debt", "total_savings"]
