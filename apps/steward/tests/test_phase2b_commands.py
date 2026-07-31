from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.steward.models import (
    Expectation,
    ReleaseTrain,
    TrackedItem,
)


class Phase2bCommandTests(TestCase):
    def test_train_open_advance_and_list(self):
        item = TrackedItem.objects.create(
            product=TrackedItem.Product.NBHD_IOS,
            kind=TrackedItem.Kind.RELEASE,
            title="iOS 2.1.6",
            status=TrackedItem.Status.ACTIVE,
            provenance="mj",
        )
        call_command(
            "steward_train",
            "--open",
            "nbhd_ios",
            "2.1.6",
            "--item",
            str(item.pk),
            stdout=StringIO(),
        )
        train = ReleaseTrain.objects.get()
        self.assertEqual(train.phase, ReleaseTrain.Phase.PLANNED)
        self.assertEqual(train.tracked_item_id, item.id)
        self.assertTrue(
            Expectation.objects.filter(
                subject="train:nbhd_ios:2.1.6",
                state=Expectation.State.ARMED,
            ).exists()
        )

        call_command(
            "steward_train",
            "--advance",
            str(train.pk),
            "pushed",
            "--sha",
            "a" * 40,
            stdout=StringIO(),
        )
        train.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.PUSHED)
        self.assertEqual(train.head_sha, "a" * 40)
        output = StringIO()
        call_command("steward_train", "--list", stdout=output)
        self.assertIn("nbhd_ios\t2.1.6\tpushed", output.getvalue())

    def test_phase2b_seed_is_idempotent_and_links_rollout_item(self):
        rollout = TrackedItem.objects.create(
            product=TrackedItem.Product.NBHD_IOS,
            kind=TrackedItem.Kind.RELEASE,
            title="iOS 2.1.5 phased rollout",
            status=TrackedItem.Status.ACTIVE,
            provenance="mj",
        )

        call_command("steward_seed_phase2b", stdout=StringIO())
        call_command("steward_seed_phase2b", stdout=StringIO())

        self.assertEqual(ReleaseTrain.objects.count(), 2)
        released = ReleaseTrain.objects.get(version_string="2.1.5")
        planned = ReleaseTrain.objects.get(version_string="2.1.6")
        self.assertEqual(released.phase, ReleaseTrain.Phase.RELEASED)
        self.assertEqual(released.tracked_item_id, rollout.id)
        self.assertEqual(planned.phase, ReleaseTrain.Phase.PLANNED)
        self.assertEqual(
            Expectation.objects.filter(
                subject="train:nbhd_ios:2.1.6",
                state=Expectation.State.ARMED,
            ).count(),
            1,
        )
