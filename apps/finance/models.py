"""Finance models — budget tracking, debt payoff, and progress snapshots."""
import uuid

from django.db import models

from apps.tenants.models import Tenant


class FinanceAccount(models.Model):
    """A debt, savings account, or asset tracked by the user."""

    class AccountType(models.TextChoices):
        CREDIT_CARD = "credit_card", "Credit Card"
        STUDENT_LOAN = "student_loan", "Student Loan"
        PERSONAL_LOAN = "personal_loan", "Personal Loan"
        MORTGAGE = "mortgage", "Mortgage"
        AUTO_LOAN = "auto_loan", "Auto Loan"
        MEDICAL_DEBT = "medical_debt", "Medical Debt"
        OTHER_DEBT = "other_debt", "Other Debt"
        SAVINGS = "savings", "Savings"
        CHECKING = "checking", "Checking"
        EMERGENCY_FUND = "emergency_fund", "Emergency Fund"

    DEBT_TYPES = {
        AccountType.CREDIT_CARD,
        AccountType.STUDENT_LOAN,
        AccountType.PERSONAL_LOAN,
        AccountType.MORTGAGE,
        AccountType.AUTO_LOAN,
        AccountType.MEDICAL_DEBT,
        AccountType.OTHER_DEBT,
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="finance_accounts"
    )
    account_type = models.CharField(max_length=32, choices=AccountType.choices)
    nickname = models.CharField(
        max_length=128, help_text="User-chosen label, e.g. 'Big CC' or 'Car Loan'"
    )
    current_balance = models.DecimalField(max_digits=12, decimal_places=2)
    original_balance = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Starting balance when first tracked (for progress %)",
    )
    interest_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Annual percentage rate",
    )
    minimum_payment = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )
    credit_limit = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    due_day = models.IntegerField(
        null=True, blank=True, help_text="Day of month payment is due (1-31)"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_accounts"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.nickname} ({self.account_type})"

    @property
    def is_debt(self) -> bool:
        return self.account_type in self.DEBT_TYPES

    @property
    def payoff_progress(self) -> float | None:
        """Percentage of debt paid off (0-100), or None if no original balance."""
        if not self.is_debt or not self.original_balance or self.original_balance <= 0:
            return None
        paid = float(self.original_balance - self.current_balance)
        return max(0.0, min(100.0, (paid / float(self.original_balance)) * 100))


class FinanceTransaction(models.Model):
    """A payment or transaction recorded against an account."""

    class TransactionType(models.TextChoices):
        PAYMENT = "payment", "Payment"
        CHARGE = "charge", "Charge"
        TRANSFER = "transfer", "Transfer"
        REFUND = "refund", "Refund"
        INTEREST = "interest", "Interest Charge"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="finance_transactions"
    )
    account = models.ForeignKey(
        FinanceAccount, on_delete=models.CASCADE, related_name="transactions"
    )
    transaction_type = models.CharField(
        max_length=16, choices=TransactionType.choices
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=256, blank=True, default="")
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finance_transactions"
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "date"]),
            models.Index(fields=["account", "date"]),
        ]

    def __str__(self) -> str:
        return f"{self.transaction_type} ${self.amount} → {self.account.nickname}"


class PayoffPlan(models.Model):
    """A saved debt payoff strategy calculation."""

    class Strategy(models.TextChoices):
        SNOWBALL = "snowball", "Snowball (lowest balance first)"
        AVALANCHE = "avalanche", "Avalanche (highest interest first)"
        HYBRID = "hybrid", "Hybrid (balanced approach)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="payoff_plans"
    )
    strategy = models.CharField(max_length=16, choices=Strategy.choices)
    monthly_budget = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Total monthly amount available for all debt payments",
    )
    total_debt = models.DecimalField(max_digits=12, decimal_places=2)
    total_interest = models.DecimalField(max_digits=12, decimal_places=2)
    payoff_months = models.IntegerField()
    payoff_date = models.DateField()
    schedule_json = models.JSONField(
        default=list,
        help_text="Month-by-month breakdown: [{month, accounts: [{nickname, balance, payment}], total_remaining}]",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_payoff_plans"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.strategy} plan — {self.payoff_months} months"


class FinanceSnapshot(models.Model):
    """Monthly point-in-time snapshot for progress tracking."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="finance_snapshots"
    )
    date = models.DateField(help_text="First of the month for this snapshot")
    total_debt = models.DecimalField(max_digits=12, decimal_places=2)
    total_savings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_payments_this_month = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    accounts_json = models.JSONField(
        default=list,
        help_text="Snapshot of all account balances: [{nickname, type, balance}]",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finance_snapshots"
        unique_together = ["tenant", "date"]
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"Snapshot {self.date} — debt ${self.total_debt}"
