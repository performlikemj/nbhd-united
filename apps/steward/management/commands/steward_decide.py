from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.steward.items import set_item_status
from apps.steward.models import Decision, EvidenceEvent, TrackedItem


class Command(BaseCommand):
    help = "Append a portfolio decision and optionally close a tracked item."

    def add_arguments(self, parser):
        parser.add_argument("--decision", required=True)
        parser.add_argument("--rationale", required=True)
        parser.add_argument("--supersedes", type=int)
        parser.add_argument("--item", type=int)
        parser.add_argument("--status", choices=TrackedItem.Status.values)

    def handle(self, *args, **options):
        if options["status"] and not options["item"]:
            raise CommandError("--status requires --item.")
        supersedes = None
        if options["supersedes"]:
            try:
                supersedes = Decision.objects.get(pk=options["supersedes"])
            except Decision.DoesNotExist as exc:
                raise CommandError("The superseded Decision does not exist.") from exc
        item = None
        if options["item"]:
            try:
                item = TrackedItem.objects.get(pk=options["item"])
            except TrackedItem.DoesNotExist as exc:
                raise CommandError("The TrackedItem does not exist.") from exc

        try:
            with transaction.atomic():
                decision = Decision(
                    decision=options["decision"],
                    rationale=options["rationale"],
                    supersedes=supersedes,
                    provenance=EvidenceEvent.Provenance.MJ,
                )
                decision.full_clean()
                decision.save()
                if item is not None:
                    set_item_status(
                        item,
                        options["status"] or TrackedItem.Status.DONE,
                        provenance=EvidenceEvent.Provenance.MJ,
                    )
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"recorded decision {decision.id}"))
