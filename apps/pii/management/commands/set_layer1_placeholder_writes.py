"""Enable or disable placeholder-at-rest Layer-1 writes for one tenant."""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Enable or disable placeholder-at-rest Layer-1 writes for one tenant."

    def add_arguments(self, parser):
        parser.add_argument("tenant_id")
        state = parser.add_mutually_exclusive_group(required=True)
        state.add_argument("--on", action="store_true", dest="turn_on")
        state.add_argument("--off", action="store_true", dest="turn_off")

    def handle(self, *args, **options):
        with transaction.atomic():
            tenant = Tenant.objects.select_for_update().get(pk=options["tenant_id"])
            tenant.layer1_placeholder_writes = bool(options["turn_on"])
            tenant.save(update_fields=["layer1_placeholder_writes"])

        state = "on" if tenant.layer1_placeholder_writes else "off"
        self.stdout.write(self.style.SUCCESS(f"tenant={tenant.id} layer1_placeholder_writes={state}"))
