"""Deprecated: per-tenant BYO flag flip — fleet-wide as of 2026-05-02.

PR #434 ships the fleet rollout (migration ``tenants.0051``) and changes
the model default to ``True``. Every non-deleted tenant has the flag set,
and new provisioning sets it automatically.

This command stays as a no-op so existing runbooks/scripts that invoke
it don't break — but it no longer mutates the DB. Use ``--disable`` if
you need to scope a single tenant *out* of BYO (e.g. emergency
rollback for one user); that is the only path that still writes.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "DEPRECATED: BYO is fleet-wide as of migration tenants.0051. Use --disable for per-tenant opt-out only."

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
            # Enable path is a no-op — the migration + model default already
            # cover every tenant. Print a deprecation note pointing at the
            # migration so an operator running this in 2027 understands.
            self.stdout.write(
                self.style.WARNING(
                    "DEPRECATED: enable_byo is now a no-op. BYO is fleet-wide "
                    "(see tenants.0051_byo_models_enabled_default_true). "
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
