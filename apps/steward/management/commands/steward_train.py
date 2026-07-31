from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

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
        parser.add_argument("--sha", help="40-character train head SHA to bind when advancing.")
        parser.add_argument(
            "--workflow",
            help=(
                "GitHub Actions workflow name to bind when advancing. Without a binding, "
                "automatic CI advance requires exactly one default-branch workflow in the collection window."
            ),
        )

    def handle(self, *args, **options):
        if options["list"]:
            if options["sha"] or options["workflow"]:
                raise CommandError("--sha and --workflow can only be used with --advance.")
            for train in ReleaseTrain.objects.all():
                self.stdout.write(
                    f"{train.id}\t{train.product}\t{train.version_string}\t{train.phase}\t"
                    f"{train.tracked_item_id or '-'}"
                )
            return

        if options["open"]:
            if options["sha"] or options["workflow"]:
                raise CommandError("--sha and --workflow can only be used with --advance.")
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
            with transaction.atomic():
                if options["sha"]:
                    sha = options["sha"].strip().lower()
                    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
                        raise CommandError("--sha must be exactly 40 hexadecimal characters.")
                    train.head_sha = sha
                    train.full_clean()
                    train.save(update_fields=["head_sha", "updated_at"])
                if options["workflow"] is not None:
                    workflow = options["workflow"].strip()
                    if not workflow or len(workflow) > 140:
                        raise CommandError("--workflow must be between 1 and 140 characters.")
                    train.ci_workflow = workflow
                    train.full_clean()
                    train.save(update_fields=["ci_workflow", "updated_at"])
                train = advance_train(
                    train,
                    phase,
                    provenance=EvidenceEvent.Provenance.MJ,
                )
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"advanced: {train.id} {train.phase}"))
