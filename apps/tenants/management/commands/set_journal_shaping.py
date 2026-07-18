"""Enable or disable the per-tenant journal-shaping capability."""

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Set the per-tenant journal-shaping capability"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            required=True,
            help="Tenant UUID (matches apps.tenants.Tenant.id)",
        )
        state = parser.add_mutually_exclusive_group(required=True)
        state.add_argument("--enable", action="store_true", help="Enable journal shaping")
        state.add_argument("--disable", action="store_true", help="Disable journal shaping")

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Tenant {tenant_id!r} not found") from exc

        tenant.journal_shaping_enabled = options["enable"]
        tenant.save(update_fields=["journal_shaping_enabled"])
        tenant.bump_pending_config()

        self.stdout.write(
            self.style.SUCCESS(f"tenant={tenant.id}: journal_shaping_enabled={tenant.journal_shaping_enabled}")
        )
        self.stdout.write(
            "Next: run `python manage.py force_apply_configs --tenant-id "
            f"{tenant.id}` to push the updated workspace files."
        )
