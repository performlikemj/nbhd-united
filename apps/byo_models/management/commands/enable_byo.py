"""Deprecated: the BYO subscription surface is parked as of 2026-08-26.

Migration ``tenants.0159`` restores the model default to ``False`` and turns
the flag off for every non-deleted tenant.

This command stays as a no-op so existing runbooks/scripts that invoke
it don't break — but it no longer mutates the DB. Use ``--disable`` if
you need to scope a single tenant *out* of BYO (e.g. emergency
rollback for one user); that is the only path that still writes.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "DEPRECATED: BYO is parked as of migration tenants.0159; this command cannot enable it."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant UUID")
        parser.add_argument(
            "--disable",
            action="store_true",
            help="Disable BYO for this tenant (per-tenant opt-out only path that still mutates state)",
        )

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(id=options["tenant"])
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Tenant {options['tenant']} not found") from exc

        if not options["disable"]:
            # Enable path is a no-op while the non-ZDR BYO surface is parked.
            self.stdout.write(
                self.style.WARNING(
                    "DEPRECATED: enable_byo is a no-op while BYO is parked "
                    "(see tenants.0159). "
                    f"Tenant {tenant.id} byo_models_enabled={tenant.byo_models_enabled}; "
                    "no changes made."
                )
            )
            return

        if not tenant.byo_models_enabled:
            self.stdout.write(self.style.WARNING(f"Tenant {tenant.id} byo_models_enabled is already False; no-op"))
            return

        tenant.byo_models_enabled = False
        tenant.save(update_fields=["byo_models_enabled"])
        self.stdout.write(self.style.SUCCESS(f"BYO subscription mode disabled for tenant {tenant.id}"))
