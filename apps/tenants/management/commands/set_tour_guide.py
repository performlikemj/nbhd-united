"""Enable, disable, or select the per-tenant tour-guide capability mode."""

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Set the per-tenant tour-guide capability and response mode"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            required=True,
            help="Tenant UUID (matches apps.tenants.Tenant.id)",
        )
        state = parser.add_mutually_exclusive_group(required=True)
        state.add_argument("--enable", action="store_true", help="Enable tour-guide mode")
        state.add_argument("--disable", action="store_true", help="Disable tour-guide mode")
        parser.add_argument(
            "--mode",
            choices=[choice.value for choice in Tenant.TourGuideMode],
            help="Response mode: cards for dev/TestFlight, links for the App Store client",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Tenant {tenant_id!r} not found") from exc

        tenant.tour_guide_enabled = options["enable"]
        update_fields = ["tour_guide_enabled"]
        if options["mode"]:
            tenant.tour_guide_mode = options["mode"]
            update_fields.append("tour_guide_mode")
        tenant.save(update_fields=update_fields)
        tenant.bump_pending_config()

        self.stdout.write(
            self.style.SUCCESS(
                f"tenant={tenant.id}: tour_guide_enabled={tenant.tour_guide_enabled} "
                f"tour_guide_mode={tenant.tour_guide_mode}"
            )
        )
        self.stdout.write(
            "Next: run `python manage.py force_apply_configs --tenant-id "
            f"{tenant.id}` to push the updated workspace files."
        )
