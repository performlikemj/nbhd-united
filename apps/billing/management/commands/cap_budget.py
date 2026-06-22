"""Toggle the global platform budget cap.

Usage:
    python manage.py cap_budget          # Cap the budget (block all messages)
    python manage.py cap_budget --uncap  # Uncap the budget (resume service)
    python manage.py cap_budget --status # Show current budget status
"""

from datetime import date

from django.core.management.base import BaseCommand

from apps.billing.models import MonthlyBudget


class Command(BaseCommand):
    help = "Cap or uncap the global platform budget for the current month."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--uncap",
            action="store_true",
            help="Uncap the budget (resume service).",
        )
        group.add_argument(
            "--status",
            action="store_true",
            help="Show current budget status without changing anything.",
        )

    def handle(self, *args, **options):
        first_of_month = date.today().replace(day=1)
        budget, created = MonthlyBudget.objects.get_or_create(
            month=first_of_month,
            defaults={"budget_dollars": 100, "spent_dollars": 0},
        )

        if options["status"]:
            self.stdout.write(
                f"Month:  {budget.month}\n"
                f"Budget: ${budget.budget_dollars}\n"
                f"Spent:  ${budget.spent_dollars}\n"
                f"Over:   {budget.is_over_budget}\n"
                f"Capped: {budget.is_capped}\n"
            )
            return

        if options["uncap"]:
            # Clear the kill-switch only. Do NOT touch spent_dollars — that
            # column tracks true month-to-date platform spend (surfaced in the
            # transparency UI and trued up hourly by reconcile_openrouter_spend),
            # so zeroing it would both wipe the accurate figure and let the next
            # reconcile re-derive remaining <= 0 and silently re-cap.
            budget.is_capped = False
            budget.save(update_fields=["is_capped"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"Budget uncapped. Spent ${budget.spent_dollars} / ${budget.budget_dollars} (unchanged)."
                )
            )
        else:
            # Engage the kill-switch only. spent_dollars stays the true figure;
            # is_capped is the enforcement signal read by check_budget.
            budget.is_capped = True
            budget.save(update_fields=["is_capped"])
            self.stdout.write(
                self.style.WARNING(
                    f"Budget capped. Spent ${budget.spent_dollars} / ${budget.budget_dollars}. "
                    "All messages will show the platform budget exceeded notice."
                )
            )
