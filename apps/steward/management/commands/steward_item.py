from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.steward.items import set_item_status
from apps.steward.models import EvidenceEvent, TrackedItem


def _parse_refs(values: list[str]) -> list[dict[str, str]]:
    refs = []
    for value in values:
        ref_type, separator, ref_value = value.partition("=")
        if not separator or not ref_type or not ref_value:
            raise CommandError("--ref must use type=value.")
        refs.append({"type": ref_type, "value": ref_value})
    return refs


class Command(BaseCommand):
    help = "Create, update, or list portfolio Steward tracked items."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true")
        parser.add_argument("--title")
        parser.add_argument("--product", choices=TrackedItem.Product.values)
        parser.add_argument("--kind", choices=TrackedItem.Kind.values)
        parser.add_argument("--status", choices=TrackedItem.Status.values)
        parser.add_argument("--context")
        parser.add_argument("--ref", action="append", default=[])

    def handle(self, *args, **options):
        if options["list"]:
            for item in TrackedItem.objects.all():
                self.stdout.write(f"{item.id}\t{item.product}\t{item.kind}\t{item.status}\t{item.title}")
            return
        for name in ("title", "product", "kind"):
            if not options[name]:
                raise CommandError(f"--{name} is required unless --list is used.")

        refs = _parse_refs(options["ref"])
        item = TrackedItem.objects.filter(
            title=options["title"],
            product=options["product"],
        ).first()
        created = item is None
        if created:
            item = TrackedItem(
                title=options["title"],
                product=options["product"],
                kind=options["kind"],
                context=options["context"] or "",
                refs=refs,
                provenance=EvidenceEvent.Provenance.MJ,
            )
        else:
            item.kind = options["kind"]
            item.provenance = EvidenceEvent.Provenance.MJ
            if options["context"] is not None:
                item.context = options["context"]
            if options["ref"]:
                item.refs = refs
        try:
            item.full_clean()
            item.save()
            if options["status"]:
                set_item_status(
                    item,
                    options["status"],
                    provenance=EvidenceEvent.Provenance.MJ,
                )
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc

        action = "created" if created else "updated"
        self.stdout.write(self.style.SUCCESS(f"{action}: {item.id} {item.title}"))
