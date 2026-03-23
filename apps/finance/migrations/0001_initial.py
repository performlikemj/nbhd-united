import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenants", "0033_tenant_finance_enabled"),
    ]

    operations = [
        migrations.CreateModel(
            name="FinanceAccount",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "account_type",
                    models.CharField(
                        choices=[
                            ("credit_card", "Credit Card"),
                            ("student_loan", "Student Loan"),
                            ("personal_loan", "Personal Loan"),
                            ("mortgage", "Mortgage"),
                            ("auto_loan", "Auto Loan"),
                            ("medical_debt", "Medical Debt"),
                            ("other_debt", "Other Debt"),
                            ("savings", "Savings"),
                            ("checking", "Checking"),
                            ("emergency_fund", "Emergency Fund"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "nickname",
                    models.CharField(
                        help_text="User-chosen label, e.g. 'Big CC' or 'Car Loan'",
                        max_length=128,
                    ),
                ),
                (
                    "current_balance",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                (
                    "original_balance",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Starting balance when first tracked (for progress %)",
                        max_digits=12,
                        null=True,
                    ),
                ),
                (
                    "interest_rate",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Annual percentage rate",
                        max_digits=5,
                        null=True,
                    ),
                ),
                (
                    "minimum_payment",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=10, null=True
                    ),
                ),
                (
                    "credit_limit",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=12, null=True
                    ),
                ),
                (
                    "due_day",
                    models.IntegerField(
                        blank=True,
                        help_text="Day of month payment is due (1-31)",
                        null=True,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="finance_accounts",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "finance_accounts",
                "ordering": ["-updated_at"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "is_active"],
                        name="finance_acc_tenant__idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="FinanceTransaction",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "transaction_type",
                    models.CharField(
                        choices=[
                            ("payment", "Payment"),
                            ("charge", "Charge"),
                            ("transfer", "Transfer"),
                            ("refund", "Refund"),
                            ("interest", "Interest Charge"),
                        ],
                        max_length=16,
                    ),
                ),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                (
                    "description",
                    models.CharField(blank=True, default="", max_length=256),
                ),
                ("date", models.DateField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transactions",
                        to="finance.financeaccount",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="finance_transactions",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "finance_transactions",
                "ordering": ["-date", "-created_at"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "date"],
                        name="finance_txn_tenant_date_idx",
                    ),
                    models.Index(
                        fields=["account", "date"],
                        name="finance_txn_acct_date_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PayoffPlan",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "strategy",
                    models.CharField(
                        choices=[
                            ("snowball", "Snowball (lowest balance first)"),
                            ("avalanche", "Avalanche (highest interest first)"),
                            ("hybrid", "Hybrid (balanced approach)"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "monthly_budget",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="Total monthly amount available for all debt payments",
                        max_digits=10,
                    ),
                ),
                (
                    "total_debt",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                (
                    "total_interest",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                ("payoff_months", models.IntegerField()),
                ("payoff_date", models.DateField()),
                (
                    "schedule_json",
                    models.JSONField(
                        default=list,
                        help_text="Month-by-month breakdown: [{month, accounts: [{nickname, balance, payment}], total_remaining}]",
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="payoff_plans",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "finance_payoff_plans",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="FinanceSnapshot",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "date",
                    models.DateField(
                        help_text="First of the month for this snapshot"
                    ),
                ),
                (
                    "total_debt",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                (
                    "total_savings",
                    models.DecimalField(
                        decimal_places=2, default=0, max_digits=12
                    ),
                ),
                (
                    "total_payments_this_month",
                    models.DecimalField(
                        decimal_places=2, default=0, max_digits=12
                    ),
                ),
                (
                    "accounts_json",
                    models.JSONField(
                        default=list,
                        help_text="Snapshot of all account balances: [{nickname, type, balance}]",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="finance_snapshots",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "finance_snapshots",
                "ordering": ["-date"],
                "unique_together": {("tenant", "date")},
            },
        ),
    ]
