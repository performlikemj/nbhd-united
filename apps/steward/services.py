from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from django.db import transaction
from django.utils import timezone

from apps.steward.models import EvidenceEvent, Expectation

MAX_EVIDENCE_PAYLOAD_BYTES = 4096
MAX_EVIDENCE_FINGERPRINT_LENGTH = 192
logger = logging.getLogger(__name__)

EvidenceIngestOutcome = Literal["created", "duplicate", "collision"]


@dataclass(frozen=True)
class EvidenceIngestResult:
    event: EvidenceEvent
    outcome: EvidenceIngestOutcome
    recovery_expectations: tuple[Expectation, ...]

    @property
    def created(self) -> bool:
        return self.outcome == "created"

    @property
    def collision(self) -> bool:
        return self.outcome == "collision"


@dataclass(frozen=True)
class EvidenceIngestInput:
    source: str
    subject: str
    occurred_at: datetime
    payload: object
    fingerprint: str
    trust: str
    provenance: str


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


def stored_evidence_fingerprint(source: str, fingerprint: str) -> str:
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("fingerprint must be a non-empty string.")
    stored = f"{source}:{fingerprint}"
    if len(stored) > MAX_EVIDENCE_FINGERPRINT_LENGTH:
        raise ValueError(f"source-prefixed fingerprint exceeds the {MAX_EVIDENCE_FINGERPRINT_LENGTH}-character limit.")
    return stored


def _event_matches_input(event: EvidenceEvent, item: EvidenceIngestInput) -> bool:
    return (
        event.source == item.source
        and event.subject == item.subject
        and event.occurred_at == item.occurred_at
        and event.payload == item.payload
        and event.trust == item.trust
        and event.provenance == item.provenance
    )


def _log_collision(
    *,
    fingerprint: str,
    event: EvidenceEvent,
    item: EvidenceIngestInput,
) -> None:
    logger.error(
        "Steward evidence fingerprint collision fingerprint=%s "
        "existing_source=%s incoming_source=%s subject_mismatch=%s content_mismatch=%s",
        fingerprint,
        event.source,
        item.source,
        event.subject != item.subject,
        not _event_matches_input(event, item),
    )


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
    item = EvidenceIngestInput(
        source=source,
        subject=subject,
        occurred_at=occurred_at,
        payload=payload,
        fingerprint=fingerprint,
        trust=trust,
        provenance=provenance,
    )
    stored_fingerprint = stored_evidence_fingerprint(source, fingerprint)
    evaluated_at = now or timezone.now()

    with transaction.atomic():
        expectation_query = (
            Expectation.objects.select_for_update().filter(subject=subject).exclude(state=Expectation.State.RETIRED)
        )
        if expectation_ids is not None:
            expectation_query = expectation_query.filter(pk__in=expectation_ids)
        locked = list(expectation_query)

        event, created = EvidenceEvent.objects.get_or_create(
            fingerprint=stored_fingerprint,
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
            collision = not _event_matches_input(event, item)
            if collision:
                _log_collision(
                    fingerprint=stored_fingerprint,
                    event=event,
                    item=item,
                )
            return EvidenceIngestResult(
                event=event,
                outcome="collision" if collision else "duplicate",
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
        outcome="created",
        recovery_expectations=recoveries,
    )


def ingest_evidence_batch(
    items: list[EvidenceIngestInput],
    *,
    now: datetime | None = None,
) -> tuple[EvidenceIngestResult, ...]:
    """Append an internal collector batch with bounded query cost."""
    if not items:
        return ()

    evaluated_at = now or timezone.now()
    normalized: list[tuple[EvidenceIngestInput, str]] = []
    seen: set[str] = set()
    for item in items:
        validate_payload_size(item.payload)
        fingerprint = stored_evidence_fingerprint(item.source, item.fingerprint)
        if fingerprint in seen:
            raise ValueError("batch fingerprints must be unique.")
        seen.add(fingerprint)
        normalized.append((item, fingerprint))

    with transaction.atomic():
        locked_expectations = list(
            Expectation.objects.select_for_update()
            .filter(subject__in={item.subject for item, _ in normalized})
            .exclude(state=Expectation.State.RETIRED)
        )
        expectations_by_subject: dict[str, list[Expectation]] = {}
        for expectation in locked_expectations:
            expectations_by_subject.setdefault(expectation.subject, []).append(expectation)

        existing = {
            event.fingerprint: event
            for event in EvidenceEvent.objects.filter(fingerprint__in=[fingerprint for _, fingerprint in normalized])
        }
        candidates = [
            EvidenceEvent(
                source=item.source,
                subject=item.subject,
                occurred_at=item.occurred_at,
                received_at=evaluated_at,
                payload=item.payload,
                fingerprint=fingerprint,
                trust=item.trust,
                provenance=item.provenance,
            )
            for item, fingerprint in normalized
            if fingerprint not in existing
        ]
        if candidates:
            EvidenceEvent.objects.bulk_create(candidates, ignore_conflicts=True)

        canonical = {
            event.fingerprint: event
            for event in EvidenceEvent.objects.filter(fingerprint__in=[fingerprint for _, fingerprint in normalized])
        }
        results: list[EvidenceIngestResult] = []
        for item, fingerprint in normalized:
            event = canonical[fingerprint]
            if fingerprint in existing:
                collision = not _event_matches_input(event, item)
                if collision:
                    _log_collision(
                        fingerprint=fingerprint,
                        event=event,
                        item=item,
                    )
                results.append(
                    EvidenceIngestResult(
                        event=event,
                        outcome="collision" if collision else "duplicate",
                        recovery_expectations=(),
                    )
                )
                continue

            if not _event_matches_input(event, item):
                _log_collision(
                    fingerprint=fingerprint,
                    event=event,
                    item=item,
                )
                results.append(
                    EvidenceIngestResult(
                        event=event,
                        outcome="collision",
                        recovery_expectations=(),
                    )
                )
                continue

            recoveries = _apply_event_to_locked_expectations(
                event=event,
                expectations=expectations_by_subject.get(item.subject, []),
                now=evaluated_at,
                targeted_mj_ack=False,
            )
            results.append(
                EvidenceIngestResult(
                    event=event,
                    outcome="created",
                    recovery_expectations=recoveries,
                )
            )

    return tuple(results)
