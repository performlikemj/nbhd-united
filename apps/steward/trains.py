from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.steward.models import (
    EvidenceEvent,
    EvidenceSource,
    Expectation,
    ReleaseTrain,
    TrackedItem,
)

TRAIN_EXPECTATION_OWNER = "release_train"
TERMINAL_PHASES = frozenset(
    {
        ReleaseTrain.Phase.RELEASED,
        ReleaseTrain.Phase.ROLLED_BACK,
    }
)
PHASE_ORDER = tuple(phase for phase in ReleaseTrain.Phase.values if phase != ReleaseTrain.Phase.ROLLED_BACK)

# Per-product next-phase SLA hours. Other products use the nbhd_united table.
SLA_HOURS = {
    TrackedItem.Product.NBHD_IOS: {
        ReleaseTrain.Phase.PLANNED: 168,
        ReleaseTrain.Phase.INTEGRATING: 72,
        ReleaseTrain.Phase.VERIFIED_LOCAL: 24,
        ReleaseTrain.Phase.PUSHED: 2,
        ReleaseTrain.Phase.CI_GREEN: 24,
        ReleaseTrain.Phase.TAGGED: 72,
        ReleaseTrain.Phase.SUBMITTED: 96,
        ReleaseTrain.Phase.IN_REVIEW: 168,
    },
    TrackedItem.Product.NBHD_UNITED: {
        ReleaseTrain.Phase.PLANNED: 168,
        ReleaseTrain.Phase.PUSHED: 1,
        ReleaseTrain.Phase.CI_GREEN: 24,
    },
}


_IOS_NEXT_PHASES = {
    ReleaseTrain.Phase.PLANNED: ReleaseTrain.Phase.INTEGRATING,
    ReleaseTrain.Phase.INTEGRATING: ReleaseTrain.Phase.VERIFIED_LOCAL,
    ReleaseTrain.Phase.VERIFIED_LOCAL: ReleaseTrain.Phase.PUSHED,
    ReleaseTrain.Phase.PUSHED: ReleaseTrain.Phase.CI_GREEN,
    ReleaseTrain.Phase.CI_GREEN: ReleaseTrain.Phase.TAGGED,
    ReleaseTrain.Phase.TAGGED: ReleaseTrain.Phase.SUBMITTED,
    ReleaseTrain.Phase.SUBMITTED: ReleaseTrain.Phase.IN_REVIEW,
    ReleaseTrain.Phase.IN_REVIEW: ReleaseTrain.Phase.RELEASED,
}
_DEFAULT_NEXT_PHASES = {
    ReleaseTrain.Phase.PLANNED: ReleaseTrain.Phase.PUSHED,
    ReleaseTrain.Phase.INTEGRATING: ReleaseTrain.Phase.PUSHED,
    ReleaseTrain.Phase.VERIFIED_LOCAL: ReleaseTrain.Phase.PUSHED,
    ReleaseTrain.Phase.PUSHED: ReleaseTrain.Phase.CI_GREEN,
    ReleaseTrain.Phase.CI_GREEN: ReleaseTrain.Phase.RELEASED,
    ReleaseTrain.Phase.TAGGED: ReleaseTrain.Phase.RELEASED,
    ReleaseTrain.Phase.SUBMITTED: ReleaseTrain.Phase.RELEASED,
    ReleaseTrain.Phase.IN_REVIEW: ReleaseTrain.Phase.RELEASED,
}


def train_subject(train: ReleaseTrain) -> str:
    return f"train:{train.product}:{train.version_string}"


def next_phase_for(train: ReleaseTrain) -> str | None:
    phases = _IOS_NEXT_PHASES if train.product == TrackedItem.Product.NBHD_IOS else _DEFAULT_NEXT_PHASES
    return phases.get(train.phase)


def _sla_hours(train: ReleaseTrain) -> int:
    table = SLA_HOURS.get(train.product, SLA_HOURS[TrackedItem.Product.NBHD_UNITED])
    if train.phase in table:
        return table[train.phase]
    if PHASE_ORDER.index(train.phase) < PHASE_ORDER.index(ReleaseTrain.Phase.PUSHED):
        return table[ReleaseTrain.Phase.PLANNED]
    return table[ReleaseTrain.Phase.CI_GREEN]


def _retire_armed_expectations(train: ReleaseTrain) -> None:
    Expectation.objects.filter(
        subject=train_subject(train),
        owner=TRAIN_EXPECTATION_OWNER,
        state__in=[Expectation.State.ARMED, Expectation.State.MISSED],
    ).update(state=Expectation.State.RETIRED)


def _arm_next_expectation(train: ReleaseTrain, *, now) -> Expectation | None:
    upcoming = next_phase_for(train)
    if upcoming is None:
        return None
    expectation = Expectation(
        kind=Expectation.Kind.DEADLINE,
        due_at=now + timedelta(hours=_sla_hours(train)),
        grace_s=6 * 60 * 60,
        evidence_source=EvidenceSource.MJ_ACK,
        subject=train_subject(train),
        state=Expectation.State.ARMED,
        on_miss=Expectation.OnMiss.DIGEST,
        owner=TRAIN_EXPECTATION_OWNER,
        subject_item=train.tracked_item,
    )
    expectation.full_clean()
    expectation.save()
    return expectation


@transaction.atomic
def open_train(
    *,
    product: str,
    version_string: str,
    tracked_item: TrackedItem | None = None,
    refs: list[dict[str, str]] | None = None,
) -> ReleaseTrain:
    train = ReleaseTrain(
        product=product,
        version_string=version_string,
        phase=ReleaseTrain.Phase.PLANNED,
        tracked_item=tracked_item,
        refs=refs or [],
    )
    train.full_clean()
    train.save()
    _arm_next_expectation(train, now=train.phase_changed_at)
    return train


@transaction.atomic
def advance_train(
    train: ReleaseTrain,
    new_phase: str,
    *,
    evidence: EvidenceEvent | None = None,
    provenance: str,
) -> ReleaseTrain:
    """Advance a release train and atomically replace its phase expectation."""
    if provenance not in {
        EvidenceEvent.Provenance.COLLECTOR,
        EvidenceEvent.Provenance.MJ,
    }:
        raise ValidationError({"provenance": "release trains may be advanced only by a collector or MJ."})
    if new_phase not in ReleaseTrain.Phase.values:
        raise ValidationError({"phase": "phase is not a valid ReleaseTrain phase."})
    if train.pk is None:
        raise ValidationError("ReleaseTrain must be saved before it can be advanced.")
    if evidence is not None and evidence.provenance != provenance:
        raise ValidationError({"evidence": "evidence provenance must match transition provenance."})

    locked = ReleaseTrain.objects.select_for_update().get(pk=train.pk)
    if locked.phase == new_phase:
        return locked
    if new_phase == ReleaseTrain.Phase.ROLLED_BACK:
        if provenance != EvidenceEvent.Provenance.MJ:
            raise ValidationError({"provenance": "only MJ may roll back a release train."})
        if PHASE_ORDER.index(locked.phase) < PHASE_ORDER.index(ReleaseTrain.Phase.PUSHED):
            raise ValidationError({"phase": "release trains may roll back only from pushed or a later phase."})
    else:
        if locked.phase == ReleaseTrain.Phase.ROLLED_BACK:
            raise ValidationError({"phase": "rolled-back release trains are terminal."})
        if PHASE_ORDER.index(new_phase) <= PHASE_ORDER.index(locked.phase):
            raise ValidationError({"phase": "release train transitions must move forward."})

    changed_at = timezone.now()
    _retire_armed_expectations(locked)
    locked.phase = new_phase
    locked.phase_changed_at = changed_at
    locked._phase_transition_allowed = True
    try:
        locked.full_clean()
        locked.save(update_fields=["phase", "phase_changed_at", "updated_at"])
    finally:
        locked._phase_transition_allowed = False
    _arm_next_expectation(locked, now=changed_at)
    return locked
