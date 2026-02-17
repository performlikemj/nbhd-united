"""Seed default cron job definitions into a tenant's workspace file share."""
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.services import seed_cron_jobs
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Seed cron jobs (morning briefing, evening check-in, background tasks) into a tenant's OpenClaw container"

    def add_arguments(self, parser):
        parser.add_argument(
            "tenant_id",
            type=str,
            help="Tenant UUID to seed cron jobs for",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]

        try:
            tenant = Tenant.objects.select_related("user").get(id=tenant_id)
        except (Tenant.DoesNotExist, ValueError) as exc:
            raise CommandError(f"Tenant not found: {tenant_id}") from exc

        if tenant.status != Tenant.Status.ACTIVE:
            raise CommandError(
                f"Tenant {tenant_id} is not active (status={tenant.status})"
            )

        self.stdout.write(
            f"Seeding cron jobs for {tenant.user.display_name} ..."
        )

        result = seed_cron_jobs(tenant)

        if result.get("skipped"):
            self.stdout.write(self.style.WARNING(
                f"Skipped: tenant already has cron jobs configured."
            ))
        elif result["errors"] == 0:
            self.stdout.write(self.style.SUCCESS(
                f"Done: {result['created']}/{result['jobs_total']} jobs seeded."
            ))
        else:
            self.stderr.write(self.style.ERROR(
                f"Partial: {result['created']}/{result['jobs_total']} seeded, "
                f"{result['errors']} errors."
            ))
