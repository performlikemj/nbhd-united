from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.steward.models import (
    EvidenceEvent,
    EvidenceSource,
    Expectation,
    ReleaseTrain,
    TrackedItem,
)
from apps.steward.trains import advance_train, open_train, train_subject


class ReleaseTrainTests(TestCase):
    def test_open_arms_ios_integrating_sla(self):
        train = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.6",
        )

        expectation = Expectation.objects.get(subject=train_subject(train))
        self.assertEqual(expectation.kind, Expectation.Kind.DEADLINE)
        self.assertEqual(expectation.evidence_source, EvidenceSource.MJ_ACK)
        self.assertEqual(expectation.subject_item_id, None)
        self.assertEqual(expectation.grace_s, 6 * 60 * 60)
        self.assertEqual(expectation.on_miss, Expectation.OnMiss.DIGEST)
        self.assertEqual(expectation.due_at, train.phase_changed_at + timedelta(hours=168))

    def test_forward_and_skip_transitions_replace_expectation(self):
        train = open_train(
            product=TrackedItem.Product.NBHD_IOS,
            version_string="2.1.6",
        )
        initial = Expectation.objects.get(subject=train_subject(train))
        now = timezone.now()

        with patch("apps.steward.trains.timezone.now", return_value=now):
            train = advance_train(
                train,
                ReleaseTrain.Phase.PUSHED,
                provenance=EvidenceEvent.Provenance.MJ,
            )

        initial.refresh_from_db()
        self.assertEqual(initial.state, Expectation.State.RETIRED)
        current = Expectation.objects.get(
            subject=train_subject(train),
            state=Expectation.State.ARMED,
        )
        self.assertEqual(train.phase, ReleaseTrain.Phase.PUSHED)
        self.assertEqual(current.evidence_source, EvidenceSource.MJ_ACK)
        self.assertEqual(current.due_at, now + timedelta(hours=2))

    def test_backward_transition_raises_without_retiring_expectation(self):
        train = open_train(
            product=TrackedItem.Product.NBHD_UNITED,
            version_string="2026.7.31",
        )
        train = advance_train(
            train,
            ReleaseTrain.Phase.CI_GREEN,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        expectation = Expectation.objects.get(
            subject=train_subject(train),
            state=Expectation.State.ARMED,
        )

        with self.assertRaises(ValidationError):
            advance_train(
                train,
                ReleaseTrain.Phase.PUSHED,
                provenance=EvidenceEvent.Provenance.MJ,
            )

        train.refresh_from_db()
        expectation.refresh_from_db()
        self.assertEqual(train.phase, ReleaseTrain.Phase.CI_GREEN)
        self.assertEqual(expectation.state, Expectation.State.ARMED)

    def test_terminal_transitions_arm_nothing_and_rollback_is_allowed(self):
        train = open_train(
            product=TrackedItem.Product.NBHD_UNITED,
            version_string="2026.8.1",
        )
        train = advance_train(
            train,
            ReleaseTrain.Phase.RELEASED,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        self.assertFalse(
            Expectation.objects.filter(
                subject=train_subject(train),
                state=Expectation.State.ARMED,
            ).exists()
        )
        train = advance_train(
            train,
            ReleaseTrain.Phase.ROLLED_BACK,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        self.assertEqual(train.phase, ReleaseTrain.Phase.ROLLED_BACK)
        self.assertFalse(
            Expectation.objects.filter(
                subject=train_subject(train),
                state=Expectation.State.ARMED,
            ).exists()
        )

    def test_rollback_requires_mj_and_a_phase_at_least_pushed(self):
        planned = open_train(
            product=TrackedItem.Product.NBHD_UNITED,
            version_string="planned-rollback",
        )
        with self.assertRaises(ValidationError):
            advance_train(
                planned,
                ReleaseTrain.Phase.ROLLED_BACK,
                provenance=EvidenceEvent.Provenance.MJ,
            )

        pushed = open_train(
            product=TrackedItem.Product.NBHD_UNITED,
            version_string="collector-rollback",
        )
        pushed = advance_train(
            pushed,
            ReleaseTrain.Phase.PUSHED,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        with self.assertRaises(ValidationError):
            advance_train(
                pushed,
                ReleaseTrain.Phase.ROLLED_BACK,
                provenance=EvidenceEvent.Provenance.COLLECTOR,
            )

        rolled_back = advance_train(
            pushed,
            ReleaseTrain.Phase.ROLLED_BACK,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        self.assertEqual(rolled_back.phase, ReleaseTrain.Phase.ROLLED_BACK)

    def test_same_phase_is_a_noop(self):
        train = open_train(
            product=TrackedItem.Product.NBHD_UNITED,
            version_string="same",
        )
        expectation_ids = list(Expectation.objects.filter(subject=train_subject(train)).values_list("id", flat=True))
        unchanged = advance_train(
            train,
            ReleaseTrain.Phase.PLANNED,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        self.assertEqual(unchanged.phase_changed_at, train.phase_changed_at)
        self.assertEqual(
            list(Expectation.objects.filter(subject=train_subject(train)).values_list("id", flat=True)),
            expectation_ids,
        )

    def test_only_collector_or_mj_may_advance(self):
        train = open_train(
            product=TrackedItem.Product.NBHD_UNITED,
            version_string="provenance",
        )

        with self.assertRaises(ValidationError):
            advance_train(
                train,
                ReleaseTrain.Phase.PUSHED,
                provenance=EvidenceEvent.Provenance.AGENT_PROPOSED,
            )

    def test_direct_phase_mutation_is_rejected(self):
        train = open_train(
            product=TrackedItem.Product.NBHD_UNITED,
            version_string="guarded",
        )
        train.phase = ReleaseTrain.Phase.PUSHED
        with self.assertRaises(ValidationError):
            train.save()
