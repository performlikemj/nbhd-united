from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.steward.models import EvidenceEvent, EvidenceSource, Expectation, TrackedItem

_POLICY_CONTEXT = (
    "Open decisions: warm-reply p50 target 15s vs ~40s reality; "
    "error_message_rate has no minimum-sample floor so 1 error in 16 turns breaches; "
    "all-skipped snapshot labeled degraded."
)

_PHASE1_ITEMS = (
    (
        "personal-openclaw-gateway",
        TrackedItem.Product.PORTFOLIO,
        TrackedItem.Kind.INFRA_WATCH,
        "Personal OpenClaw gateway",
    ),
    (
        "nbhd-ios-2.1.5-rollout",
        TrackedItem.Product.NBHD_IOS,
        TrackedItem.Kind.RELEASE,
        "iOS 2.1.5 phased rollout",
    ),
    (
        "nbhd-united-main-ci",
        TrackedItem.Product.NBHD_UNITED,
        TrackedItem.Kind.RECURRING,
        "nbhd-united weekly CI green",
    ),
)


def _item(
    *,
    title: str,
    product: str,
    kind: str,
    context: str = "",
) -> TrackedItem:
    item = TrackedItem.objects.filter(title=title, product=product).first()
    if item is None:
        item = TrackedItem(
            title=title,
            product=product,
            kind=kind,
            context=context,
            status=TrackedItem.Status.ACTIVE,
            provenance=EvidenceEvent.Provenance.MJ,
        )
        item.full_clean()
        item.save()
        return item
    changed = []
    if item.kind != kind:
        item.kind = kind
        changed.append("kind")
    if context and item.context != context:
        item.context = context
        changed.append("context")
    if changed:
        item.provenance = EvidenceEvent.Provenance.MJ
        changed.extend(["provenance", "updated_at"])
        item.full_clean()
        item.save(update_fields=changed)
    return item


class Command(BaseCommand):
    help = "Idempotently seed the Steward Phase 2a PM ledger."

    def handle(self, *args, **options):
        now = timezone.now()
        policy_item = _item(
            title="Eval/SLO alert policy review",
            product=TrackedItem.Product.PORTFOLIO,
            kind=TrackedItem.Kind.BLOCKED_ON_MJ,
            context=_POLICY_CONTEXT,
        )
        expectation = Expectation.objects.filter(
            kind=Expectation.Kind.DEADLINE,
            subject="decision:eval-slo-policy",
        ).first()
        if expectation is None:
            expectation = Expectation(
                kind=Expectation.Kind.DEADLINE,
                due_at=now + timedelta(days=3),
                grace_s=86400,
                evidence_source=EvidenceSource.MJ_ACK,
                subject="decision:eval-slo-policy",
                state=Expectation.State.ARMED,
                on_miss=Expectation.OnMiss.DIGEST,
                owner="mj",
                subject_item=policy_item,
            )
            expectation.full_clean()
            expectation.save()
        elif expectation.subject_item_id != policy_item.id:
            expectation.subject_item = policy_item
            expectation.full_clean()
            expectation.save(update_fields=["subject_item"])

        for subject, product, kind, title in _PHASE1_ITEMS:
            phase1_expectation = Expectation.objects.filter(subject=subject).first()
            if phase1_expectation is None:
                self.stdout.write(f"skipped missing expectation: {subject}")
                continue
            item = _item(title=title, product=product, kind=kind)
            if phase1_expectation.subject_item_id != item.id:
                phase1_expectation.subject_item = item
                phase1_expectation.full_clean()
                phase1_expectation.save(update_fields=["subject_item"])
            self.stdout.write(f"linked: {subject} -> {item.id}")

        self.stdout.write(self.style.SUCCESS("Steward Phase 2a ledger seeded."))
