"""Manually reconcile monthly donation disbursements.

The ``snapshot-donations-monthly`` cron writes one PENDING ``DonationLedger`` row
per revenue-generating tenant (paying subscribers and credit top-up buyers) for
the just-closed month (see ``apps.billing.donation_service``). Disbursement is
manual (MVP): donate the listed total to the food initiative out of band, then
run this command with the receipt to flip those rows PENDING → COMPLETED so the
ledger becomes an auditable record of money actually sent. This command only
RECORDS the disbursement — it never moves money.

Usage:
    python manage.py reconcile_donations                          # list pending for the last closed month
    python manage.py reconcile_donations --month 2026-06           # list pending for a specific month
    python manage.py reconcile_donations --month 2026-06 \\
        --complete --receipt "bank transfer #ABC123"               # mark that month's rows disbursed
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum
from django.utils import timezone

from apps.billing.donation_service import _closed_month_first
from apps.billing.models import DonationLedger


class Command(BaseCommand):
    help = "List or disburse (mark completed) pending monthly donations."

    def add_arguments(self, parser):
        parser.add_argument("--month", help="Target month YYYY-MM (default: last closed month).")
        parser.add_argument(
            "--complete",
            action="store_true",
            help="Mark that month's PENDING donations COMPLETED. Requires --receipt.",
        )
        parser.add_argument(
            "--receipt",
            default="",
            help="Receipt / transaction reference recorded on each completed row.",
        )

    def _target_month(self, month_arg: str | None) -> date:
        if month_arg:
            try:
                return datetime.strptime(month_arg, "%Y-%m").date().replace(day=1)
            except ValueError as exc:
                raise CommandError(f"--month must be YYYY-MM, got {month_arg!r}") from exc
        return _closed_month_first(timezone.now().date())

    def handle(self, *args, **options):
        month = self._target_month(options["month"])
        pending = DonationLedger.objects.filter(month=month, status=DonationLedger.Status.PENDING)

        if options["complete"]:
            receipt = options["receipt"].strip()
            if not receipt:
                raise CommandError("--complete requires --receipt <reference>.")
            total = pending.aggregate(t=Sum("donation_amount"))["t"] or Decimal("0")
            updated = pending.update(status=DonationLedger.Status.COMPLETED, receipt_reference=receipt)
            if updated == 0:
                self.stdout.write(self.style.WARNING(f"No pending donations for {month:%Y-%m}. Nothing to complete."))
                return
            self.stdout.write(
                self.style.SUCCESS(
                    f"Marked {updated} donation(s) for {month:%Y-%m} COMPLETED "
                    f"(${total.quantize(Decimal('0.01'))}) with receipt: {receipt}"
                )
            )
            return

        rows = list(pending.order_by("-donation_amount"))
        if not rows:
            self.stdout.write(f"No pending donations for {month:%Y-%m}.")
            return

        total = sum((r.donation_amount for r in rows), Decimal("0"))
        self.stdout.write(f"Pending donations for {month:%Y-%m} ({len(rows)} tenant(s)):")
        for r in rows:
            self.stdout.write(
                f"  {r.tenant_id}  ${r.donation_amount}  (revenue base ${r.surplus_amount}, {r.donation_percentage}%)"
            )
        self.stdout.write(self.style.SUCCESS(f"TOTAL to disburse: ${total.quantize(Decimal('0.01'))}"))
        self.stdout.write(
            f'After donating, run: manage.py reconcile_donations --month {month:%Y-%m} --complete --receipt "<ref>"'
        )
