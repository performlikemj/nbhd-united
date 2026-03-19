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
            "--uncap", action="store_true",
            help="Uncap the budget (resume service).",
        )
        group.add_argument(
            "--status", action="store_true",
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
                f"Month: {budget.month}\n"
                f"Budget: ${budget.budget_dollars}\n"
                f"Spent:  ${budget.spent_dollars}\n"
                f"Over:   {budget.is_over_budget}\n"
            )
            return

        if options["uncap"]:
            # Set spent to 0 to uncap
            budget.spent_dollars = 0
            budget.save(update_fields=["spent_dollars"])
            self.stdout.write(self.style.SUCCESS(
                f"Budget uncapped. Spent reset to $0 / ${budget.budget_dollars}."
            ))
        else:
            # Set spent = budget to cap
            budget.spent_dollars = budget.budget_dollars
            budget.save(update_fields=["spent_dollars"])
            self.stdout.write(self.style.WARNING(
                f"Budget capped. Spent set to ${budget.spent_dollars} / ${budget.budget_dollars}. "
                "All messages will show the platform budget exceeded notice."
            ))
