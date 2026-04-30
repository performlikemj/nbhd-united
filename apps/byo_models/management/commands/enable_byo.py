"""Enable or disable the BYO subscription feature flag for a single tenant.

Used for canary rollout — flip the flag on for `oc-148ccf1c-...` (the
admin's tenant), validate end-to-end, then flip more tenants on as we
gain confidence.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Enable BYO subscription mode for a single tenant (canary rollout)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant UUID")
        parser.add_argument(
            "--disable",
            action="store_true",
            help="Disable instead of enable",
        )

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(id=options["tenant"])
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Tenant {options['tenant']} not found") from exc

        new_value = not options["disable"]
        if tenant.byo_models_enabled == new_value:
            self.stdout.write(
                self.style.WARNING(f"Tenant {tenant.id} byo_models_enabled is already {new_value}; no-op")
            )
            return

        tenant.byo_models_enabled = new_value
        tenant.save(update_fields=["byo_models_enabled"])
        action = "enabled" if new_value else "disabled"
        self.stdout.write(self.style.SUCCESS(f"BYO subscription mode {action} for tenant {tenant.id}"))
