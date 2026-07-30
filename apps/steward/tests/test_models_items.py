from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.steward.items import set_item_status
from apps.steward.models import (
    Decision,
    DependencyEdge,
    EvidenceEvent,
    TrackedItem,
)


def _item(title="Item") -> TrackedItem:
    return TrackedItem.objects.create(
        product=TrackedItem.Product.PORTFOLIO,
        kind=TrackedItem.Kind.WORK,
        title=title,
        provenance=EvidenceEvent.Provenance.MJ,
    )


class StewardLedgerModelTests(TestCase):
    def test_tracked_item_caps_context_and_validates_refs(self):
        item = TrackedItem(
            product=TrackedItem.Product.PORTFOLIO,
            kind=TrackedItem.Kind.WORK,
            title="Bounded",
            context="x" * 2001,
            refs=[{"type": "bad", "value": "x"}],
            provenance=EvidenceEvent.Provenance.MJ,
        )
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_decision_is_append_only(self):
        decision = Decision.objects.create(
            decision="Ship it",
            rationale="Verified",
            provenance=EvidenceEvent.Provenance.MJ,
        )
        decision.rationale = "Changed"
        with self.assertRaises(ValidationError):
            decision.save()
        with self.assertRaises(ValidationError):
            decision.delete()

    def test_dependency_rejects_self_edge(self):
        item = _item()
        edge = DependencyEdge(
            from_item=item,
            to_item=item,
            kind=DependencyEdge.Kind.BLOCKS,
        )
        with self.assertRaises(ValidationError):
            edge.full_clean()

    def test_status_service_updates_changed_at_and_reason(self):
        item = _item()
        previous = timezone.now() - timedelta(days=2)
        TrackedItem.objects.filter(pk=item.pk).update(status_changed_at=previous)
        item.refresh_from_db()

        set_item_status(
            item,
            TrackedItem.Status.BLOCKED,
            provenance=EvidenceEvent.Provenance.MJ,
            reason="Needs approval",
        )

        item.refresh_from_db()
        self.assertGreater(item.status_changed_at, previous)
        self.assertEqual(item.blocked_reason, "Needs approval")
