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
        state = parser.add_mutually_exclusive_group()
        state.add_argument("--enable", action="store_true", help="Enable tour-guide mode")
        state.add_argument("--disable", action="store_true", help="Disable tour-guide mode")
        manifest = parser.add_mutually_exclusive_group()
        manifest.add_argument(
            "--manifest-ok",
            action="store_const",
            const=True,
            dest="manifest_ok",
            help="Mark the tenant image's settings-tools manifest as tour-guide capable",
        )
        manifest.add_argument(
            "--manifest-not-ok",
            action="store_const",
            const=False,
            dest="manifest_ok",
            help="Mark the tenant image's settings-tools manifest as not tour-guide capable",
        )
        parser.add_argument(
            "--mode",
            choices=[choice.value for choice in Tenant.TourGuideMode],
            help="Response mode: cards for dev/TestFlight, links for the App Store client",
        )

    def handle(self, *args, **options):
        if not (options["enable"] or options["disable"] or options["mode"] or options["manifest_ok"] is not None):
            raise CommandError("Specify at least one tour-guide setting to update")

        tenant_id = options["tenant_id"]
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Tenant {tenant_id!r} not found") from exc

        update_fields = []
        if options["enable"] or options["disable"]:
            tenant.tour_guide_enabled = options["enable"]
            update_fields.append("tour_guide_enabled")
        if options["mode"]:
            tenant.tour_guide_mode = options["mode"]
            update_fields.append("tour_guide_mode")
        if options["manifest_ok"] is not None:
            tenant.tour_guide_manifest_ok = options["manifest_ok"]
            update_fields.append("tour_guide_manifest_ok")
        tenant.save(update_fields=update_fields)
        tenant.bump_pending_config()

        self.stdout.write(
            self.style.SUCCESS(
                f"tenant={tenant.id}: tour_guide_enabled={tenant.tour_guide_enabled} "
                f"tour_guide_mode={tenant.tour_guide_mode} "
                f"tour_guide_manifest_ok={tenant.tour_guide_manifest_ok}"
            )
        )
        self.stdout.write(
            "Next: run `python manage.py force_apply_configs --tenant-id "
            f"{tenant.id}` to push the updated workspace files."
        )
