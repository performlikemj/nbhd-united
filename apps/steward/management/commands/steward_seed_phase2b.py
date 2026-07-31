from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.steward.models import EvidenceEvent, ReleaseTrain, TrackedItem
from apps.steward.trains import advance_train, open_train


class Command(BaseCommand):
    help = "Idempotently seed the Steward Phase 2b release trains."

    def handle(self, *args, **options):
        rollout_item = TrackedItem.objects.filter(title="iOS 2.1.5 phased rollout").first()
        released, created = ReleaseTrain.objects.get_or_create(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.5",
            defaults={
                "phase": ReleaseTrain.Phase.RELEASED,
                "tracked_item": rollout_item,
            },
        )
        if not created and released.phase != ReleaseTrain.Phase.RELEASED:
            released = advance_train(
                released,
                ReleaseTrain.Phase.RELEASED,
                provenance=EvidenceEvent.Provenance.MJ,
            )
        if released.tracked_item_id != getattr(rollout_item, "pk", None):
            released.tracked_item = rollout_item
            released.full_clean()
            released.save(update_fields=["tracked_item", "updated_at"])
        self.stdout.write(f"{'created' if created else 'present'}: nbhd_ios 2.1.5 released")

        planned = ReleaseTrain.objects.filter(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.6",
        ).first()
        if planned is None:
            planned = open_train(
                product=TrackedItem.Product.NBHD_IOS,
                version_string="2.1.6",
            )
            action = "created"
        else:
            action = "present"
        self.stdout.write(f"{action}: nbhd_ios 2.1.6 {planned.phase}")
        self.stdout.write(self.style.SUCCESS("Steward Phase 2b release trains seeded."))
