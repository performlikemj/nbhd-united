from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.steward.models import EvidenceEvent, Expectation

MAX_EVIDENCE_PAYLOAD_BYTES = 4096


@dataclass(frozen=True)
class EvidenceIngestResult:
    event: EvidenceEvent
    created: bool
    recovery_expectations: tuple[Expectation, ...]


def validate_payload_size(payload: object) -> None:
    try:
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be valid JSON.") from exc
    if len(encoded) > MAX_EVIDENCE_PAYLOAD_BYTES:
        raise ValueError("payload exceeds the 4096-byte limit.")


def generated_fingerprint(
    *,
    source: str,
    subject: str,
    occurred_at: datetime,
    payload: object,
) -> str:
    material = json.dumps(
        {
            "source": source,
            "subject": subject,
            "occurred_at": occurred_at.isoformat(),
            "payload": payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _apply_event_to_locked_expectations(
    *,
    event: EvidenceEvent,
    expectations: list[Expectation],
    now: datetime,
    targeted_mj_ack: bool,
) -> tuple[Expectation, ...]:
    recoveries: list[Expectation] = []
    is_mj_ack = event.source == "mj_ack" and event.provenance == EvidenceEvent.Provenance.MJ
    for expectation in expectations:
        source_matches = expectation.evidence_source == event.source or targeted_mj_ack
        if expectation.kind == Expectation.Kind.DEADLINE:
            if not source_matches and not is_mj_ack:
                continue
            expectation.last_satisfied_at = event.occurred_at
            expectation.state = Expectation.State.SATISFIED
            expectation.last_alerted_at = None
            expectation.save(update_fields=["last_satisfied_at", "state", "last_alerted_at"])
            continue

        if not source_matches or not expectation.interval_s:
            continue
        if expectation.last_satisfied_at is not None and event.occurred_at <= expectation.last_satisfied_at:
            continue

        expectation.last_satisfied_at = event.occurred_at
        current_through = event.occurred_at + timedelta(seconds=expectation.interval_s + expectation.grace_s)
        update_fields = ["last_satisfied_at"]
        if now <= current_through:
            was_missed = expectation.state == Expectation.State.MISSED
            expectation.state = Expectation.State.ARMED
            expectation.last_alerted_at = None
            update_fields.extend(["state", "last_alerted_at"])
            if (
                was_missed
                and expectation.kind == Expectation.Kind.HEARTBEAT
                and expectation.on_miss == Expectation.OnMiss.URGENT
            ):
                recoveries.append(expectation)
        expectation.save(update_fields=update_fields)
    return tuple(recoveries)


def ingest_evidence(
    *,
    source: str,
    subject: str,
    occurred_at: datetime,
    payload: object,
    fingerprint: str,
    trust: str,
    provenance: str,
    now: datetime | None = None,
    expectation_ids: tuple[int, ...] | None = None,
) -> EvidenceIngestResult:
    """Append evidence and atomically apply it to matching expectations."""
    validate_payload_size(payload)
    evaluated_at = now or timezone.now()

    with transaction.atomic():
        expectation_query = (
            Expectation.objects.select_for_update().filter(subject=subject).exclude(state=Expectation.State.RETIRED)
        )
        if expectation_ids is not None:
            expectation_query = expectation_query.filter(pk__in=expectation_ids)
        locked = list(expectation_query)

        event, created = EvidenceEvent.objects.get_or_create(
            fingerprint=fingerprint,
            defaults={
                "source": source,
                "subject": subject,
                "occurred_at": occurred_at,
                "received_at": evaluated_at,
                "payload": payload,
                "trust": trust,
                "provenance": provenance,
            },
        )

        if not created:
            return EvidenceIngestResult(
                event=event,
                created=False,
                recovery_expectations=(),
            )

        recoveries = _apply_event_to_locked_expectations(
            event=event,
            expectations=locked,
            now=evaluated_at,
            targeted_mj_ack=(
                expectation_ids is not None
                and event.source == "mj_ack"
                and event.provenance == EvidenceEvent.Provenance.MJ
            ),
        )

    return EvidenceIngestResult(
        event=event,
        created=True,
        recovery_expectations=recoveries,
    )
