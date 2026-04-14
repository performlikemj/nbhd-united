import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0024_tenant_donation_fields"),
        ("billing", "0003_reconcile_refactor"),
    ]

    operations = [
        migrations.CreateModel(
            name="DonationLedger",
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
                    "month",
                    models.DateField(help_text="First day of the month"),
                ),
                (
                    "surplus_amount",
                    models.DecimalField(
                        decimal_places=4,
                        default=0,
                        help_text="Total surplus for the month",
                        max_digits=10,
                    ),
                ),
                (
                    "donation_amount",
                    models.DecimalField(
                        decimal_places=4,
                        default=0,
                        help_text="Amount allocated to donation (surplus * percentage)",
                        max_digits=10,
                    ),
                ),
                (
                    "donation_percentage",
                    models.IntegerField(
                        default=100,
                        help_text="Snapshot of tenant's donation_percentage at calculation time",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                (
                    "receipt_reference",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="External receipt or transaction reference",
                        max_length=255,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="donation_ledger",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "donation_ledger",
                "unique_together": {("tenant", "month")},
            },
        ),
        migrations.AddIndex(
            model_name="donationledger",
            index=models.Index(
                fields=["month", "status"],
                name="donation_le_month_8062ee_idx",
            ),
        ),
    ]
