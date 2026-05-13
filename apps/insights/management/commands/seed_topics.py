"""Seed the topic registry for Gravity and Fuel. Idempotent."""

from django.core.management.base import BaseCommand

from apps.insights.seed import seed_topics


class Command(BaseCommand):
    help = "Seed canonical topics for the assistant baseline (idempotent)."

    def handle(self, *args, **options):
        counts = seed_topics()
        for key, n in counts.items():
            self.stdout.write(f"{key}: {n} created")
        self.stdout.write(self.style.SUCCESS("Topic seed complete."))
