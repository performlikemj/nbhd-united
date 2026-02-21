from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.services import seed_cron_jobs


class Command(BaseCommand):
    help = "Seed default cron jobs for a tenant's OpenClaw container"

    def add_arguments(self, parser):
        parser.add_argument("tenant_id", type=str, help="Tenant UUID")

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        try:
            result = seed_cron_jobs(tenant_id)
        except Exception as exc:
            raise CommandError(f"Failed to seed cron jobs: {exc}") from exc

        self.stdout.write(f"Result: {result}")
        if result.get("skipped"):
            self.stdout.write(self.style.WARNING(
                f"Tenant already has cron jobs — skipped (found existing jobs)"
            ))
        elif result.get("created", 0) > 0:
            self.stdout.write(self.style.SUCCESS(
                f"Created {result['created']} cron jobs for tenant {tenant_id}"
            ))
        else:
            self.stdout.write(self.style.WARNING("No jobs created"))
