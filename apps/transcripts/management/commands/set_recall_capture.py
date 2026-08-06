from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Enable or disable forward-only transcript capture for one tenant."

    def add_arguments(self, parser):
        parser.add_argument("tenant_id")
        state = parser.add_mutually_exclusive_group(required=True)
        state.add_argument("--on", action="store_true", dest="turn_on")
        state.add_argument("--off", action="store_true", dest="turn_off")

    def handle(self, *args, **options):
        with transaction.atomic():
            tenant = Tenant.objects.select_for_update().get(pk=options["tenant_id"])
            update_fields = ["recall_capture_enabled"]
            if options["turn_on"]:
                tenant.recall_capture_enabled = True
                if tenant.recall_capture_birthday is None:
                    tenant.recall_capture_birthday = timezone.now()
                    update_fields.append("recall_capture_birthday")
            else:
                tenant.recall_capture_enabled = False
            tenant.save(update_fields=update_fields)

        state = "on" if tenant.recall_capture_enabled else "off"
        birthday = tenant.recall_capture_birthday.isoformat() if tenant.recall_capture_birthday else "not-set"
        self.stdout.write(self.style.SUCCESS(f"tenant={tenant.id} recall_capture={state} birthday={birthday}"))
