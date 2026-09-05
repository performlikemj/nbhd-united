from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.steward.models import EvidenceEvent, EvidenceSource, Expectation, TrackedItem


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError
    return parsed


class Command(BaseCommand):
    help = "Idempotently create a Steward heartbeat expectation and its infrastructure watch item."

    def add_arguments(self, parser):
        parser.add_argument("--subject", required=True)
        parser.add_argument("--interval", required=True, type=_positive_int)
        parser.add_argument("--grace", required=True, type=_positive_int)
        parser.add_argument("--title", required=True)

    def handle(self, *args, **options):
        now = timezone.now()
        subject = options["subject"].strip()
        title = options["title"].strip()
        if not subject or len(subject) > 128:
            raise CommandError("--subject must be a non-empty string of at most 128 characters.")
        if not title or len(title) > 200:
            raise CommandError("--title must be a non-empty string of at most 200 characters.")

        try:
            with transaction.atomic():
                item, item_created = TrackedItem.objects.get_or_create(
                    product=TrackedItem.Product.PORTFOLIO,
                    kind=TrackedItem.Kind.INFRA_WATCH,
                    title=title,
                    defaults={
                        "status": TrackedItem.Status.ACTIVE,
                        "provenance": EvidenceEvent.Provenance.MJ,
                    },
                )
                expectation, expectation_created = Expectation.objects.get_or_create(
                    kind=Expectation.Kind.HEARTBEAT,
                    subject=subject,
                    defaults={
                        "interval_s": options["interval"],
                        "grace_s": options["grace"],
                        "evidence_source": EvidenceSource.GATEWAY_HEARTBEAT,
                        "state": Expectation.State.ARMED,
                        "last_satisfied_at": now,
                        "on_miss": Expectation.OnMiss.URGENT,
                        "owner": "mj",
                        "subject_item": item,
                    },
                )
                if expectation_created:
                    expectation.full_clean()
                if item_created:
                    item.full_clean()
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc

        item_action = "created" if item_created else "present"
        expectation_action = "created" if expectation_created else "present"
        self.stdout.write(f"{item_action}: tracked-item:{item.pk}")
        self.stdout.write(f"{expectation_action}: expectation:{expectation.pk}")
        self.stdout.write(self.style.SUCCESS("Steward heartbeat expectation ready."))
