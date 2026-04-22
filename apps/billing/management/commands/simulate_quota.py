"""Simulate quota enforcement for testing.

Set a tenant's estimated_cost_this_month to just above their budget
to trigger quota enforcement on the next message, or reset it.

Usage:
    python manage.py simulate_quota <tenant_prefix> --trigger
    python manage.py simulate_quota <tenant_prefix> --reset
    python manage.py simulate_quota <tenant_prefix> --status
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.billing.services import check_budget
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Simulate quota enforcement by setting cost above/below budget"

    def add_arguments(self, parser):
        parser.add_argument(
            "tenant_prefix",
            type=str,
            help="First 8+ chars of tenant UUID",
        )
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--trigger",
            action="store_true",
            help="Set cost to budget + $0.01 (triggers enforcement on next message)",
        )
        group.add_argument(
            "--reset",
            action="store_true",
            help="Reset cost to $0.00 (clears enforcement)",
        )
        group.add_argument(
            "--status",
            action="store_true",
            help="Show current budget status without changes",
        )

    def handle(self, *args, **options):
        prefix = options["tenant_prefix"]
        tenant = Tenant.objects.filter(id__startswith=prefix).first()
        if not tenant:
            raise CommandError(f"No tenant found matching prefix '{prefix}'")

        budget = tenant.effective_cost_budget
        current = tenant.estimated_cost_this_month

        if options["status"]:
            self._show_status(tenant, budget, current)
            return

        if options["trigger"]:
            new_cost = budget + Decimal("0.01")
            Tenant.objects.filter(id=tenant.id).update(estimated_cost_this_month=new_cost)
            tenant.refresh_from_db()
            self.stdout.write(self.style.WARNING(f"Set estimated_cost_this_month to ${new_cost} (budget: ${budget})"))
            self.stdout.write(
                "Next message from this tenant will:\n"
                "  1. Return budget exhaustion error\n"
                "  2. Hibernate their container (0 replicas)\n"
                "  3. Block further messages until reset or month rolls over"
            )
            reason = check_budget(tenant)
            self.stdout.write(f"check_budget() returns: '{reason}'")

        elif options["reset"]:
            Tenant.objects.filter(id=tenant.id).update(
                estimated_cost_this_month=Decimal("0"),
            )
            tenant.refresh_from_db()
            self.stdout.write(self.style.SUCCESS(f"Reset estimated_cost_this_month to $0.00 (budget: ${budget})"))
            # If tenant was hibernated for quota, they'll wake on next message
            # via the normal wake-on-message flow
            if tenant.hibernated_at:
                self.stdout.write(
                    "Tenant is still hibernated — they'll wake automatically when they send their next message."
                )
            reason = check_budget(tenant)
            self.stdout.write(f"check_budget() returns: '{reason or '(clear)'}' ")

        self.stdout.write("")
        self._show_status(tenant, tenant.effective_cost_budget, tenant.estimated_cost_this_month)

    def _show_status(self, tenant, budget, current):
        over = tenant.is_over_budget
        reason = check_budget(tenant)
        self.stdout.write(f"Tenant:      {tenant.id}")
        self.stdout.write(f"Container:   {tenant.container_id or '(none)'}")
        self.stdout.write(f"Status:      {tenant.status}")
        self.stdout.write(f"Hibernated:  {tenant.hibernated_at or 'no'}")
        self.stdout.write(f"Budget:      ${budget}")
        self.stdout.write(f"Cost MTD:    ${current}")
        pct = round(float(current) / float(budget) * 100, 1) if budget else 0
        self.stdout.write(f"Usage:       {pct}%")
        style = self.style.ERROR if over else self.style.SUCCESS
        self.stdout.write(style(f"Over budget: {over}"))
        self.stdout.write(f"check_budget: '{reason or '(clear)'}'")
