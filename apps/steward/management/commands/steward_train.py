from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.steward.models import EvidenceEvent, ReleaseTrain, TrackedItem
from apps.steward.trains import advance_train, open_train


class Command(BaseCommand):
    help = "Open, advance, or list Steward release trains."

    def add_arguments(self, parser):
        actions = parser.add_mutually_exclusive_group(required=True)
        actions.add_argument("--open", nargs=2, metavar=("PRODUCT", "VERSION"))
        actions.add_argument("--advance", nargs=2, metavar=("ID", "PHASE"))
        actions.add_argument("--list", action="store_true")
        parser.add_argument("--item", type=int, help="TrackedItem id to link when opening a train.")

    def handle(self, *args, **options):
        if options["list"]:
            for train in ReleaseTrain.objects.all():
                self.stdout.write(
                    f"{train.id}\t{train.product}\t{train.version_string}\t{train.phase}\t"
                    f"{train.tracked_item_id or '-'}"
                )
            return

        if options["open"]:
            product, version = options["open"]
            if product not in TrackedItem.Product.values:
                raise CommandError("Product is not a valid TrackedItem product.")
            if ReleaseTrain.objects.filter(product=product, version_string=version).exists():
                raise CommandError("A release train already exists for this product and version.")
            item = None
            if options["item"]:
                try:
                    item = TrackedItem.objects.get(pk=options["item"])
                except TrackedItem.DoesNotExist as exc:
                    raise CommandError("The TrackedItem does not exist.") from exc
            try:
                train = open_train(
                    product=product,
                    version_string=version,
                    tracked_item=item,
                )
            except ValidationError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(self.style.SUCCESS(f"opened: {train.id} {train.product} {train.version_string}"))
            return

        if options["item"]:
            raise CommandError("--item can only be used with --open.")
        train_id, phase = options["advance"]
        try:
            train = ReleaseTrain.objects.get(pk=int(train_id))
        except (ValueError, ReleaseTrain.DoesNotExist) as exc:
            raise CommandError("ReleaseTrain does not exist.") from exc
        try:
            train = advance_train(
                train,
                phase,
                provenance=EvidenceEvent.Provenance.MJ,
            )
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"advanced: {train.id} {train.phase}"))
