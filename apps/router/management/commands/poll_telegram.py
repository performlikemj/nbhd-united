"""Django management command to run the central Telegram poller."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the central Telegram poller (long-polling getUpdates)"

    def handle(self, *args, **options):
        from apps.router.poller import TelegramPoller

        self.stdout.write(self.style.SUCCESS("Starting central Telegram poller…"))
        poller = TelegramPoller()
        poller.start()
        self.stdout.write(self.style.SUCCESS("Poller stopped."))
