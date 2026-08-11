from django.core.management.base import BaseCommand

from apps.datebook.services import sweep_device_commands


class Command(BaseCommand):
    help = "Requeue pre-start expired leases, expire never-started commands, and mark stale execution ambiguous."

    def handle(self, *args, **options):
        counts = sweep_device_commands()
        self.stdout.write(
            self.style.SUCCESS(
                "datebook sweep "
                f"requeued={counts['requeued']} expired={counts['expired']} ambiguous={counts['ambiguous']}"
            )
        )
