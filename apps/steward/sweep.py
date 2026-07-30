from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.steward.models import EvidenceEvent, Expectation
from apps.steward.notify import send_urgent

logger = logging.getLogger(__name__)

URGENT_ALERT_COOLDOWN = timedelta(hours=6)


@dataclass(frozen=True)
class PendingNotice:
    expectation_id: int
    subject: str
    text: str
    fingerprint: str


def _latest_trusted_evidence(
    expectations: list[Expectation],
    now: datetime,
) -> dict[tuple[str, str, str], datetime]:
    subjects = {expectation.subject for expectation in expectations}
    sources = {expectation.evidence_source for expectation in expectations}
    sources.add("mj_ack")
    if not subjects:
        return {}

    rows = (
        EvidenceEvent.objects.filter(
            subject__in=subjects,
            source__in=sources,
            occurred_at__lte=now,
            provenance__in=[
                EvidenceEvent.Provenance.MJ,
                EvidenceEvent.Provenance.COLLECTOR,
            ],
        )
        .values("source", "subject", "provenance")
        .annotate(latest=Max("occurred_at"))
    )
    return {(row["source"], row["subject"], row["provenance"]): row["latest"] for row in rows}


def _matching_latest(
    expectation: Expectation,
    evidence: dict[tuple[str, str, str], datetime],
) -> datetime | None:
    candidates = [
        evidence.get(
            (
                expectation.evidence_source,
                expectation.subject,
                EvidenceEvent.Provenance.COLLECTOR,
            )
        ),
        evidence.get(
            (
                expectation.evidence_source,
                expectation.subject,
                EvidenceEvent.Provenance.MJ,
            )
        ),
    ]
    if expectation.kind == Expectation.Kind.DEADLINE:
        candidates.append(
            evidence.get(
                (
                    "mj_ack",
                    expectation.subject,
                    EvidenceEvent.Provenance.MJ,
                )
            )
        )
    present = [candidate for candidate in candidates if candidate is not None]
    return max(present) if present else None


def _claim_sweep_work(now: datetime) -> tuple[list[PendingNotice], int]:
    notices: list[PendingNotice] = []
    digest_misses = 0
    with transaction.atomic():
        expectations = list(
            Expectation.objects.select_for_update(skip_locked=True).exclude(state=Expectation.State.RETIRED)
        )
        evidence = _latest_trusted_evidence(expectations, now)
        changed: list[Expectation] = []

        for expectation in expectations:
            original = (
                expectation.state,
                expectation.last_satisfied_at,
                expectation.miss_count,
                expectation.last_alerted_at,
            )
            latest = _matching_latest(expectation, evidence)

            if expectation.kind == Expectation.Kind.DEADLINE:
                if expectation.state == Expectation.State.SATISFIED:
                    continue
                if latest is not None:
                    expectation.last_satisfied_at = latest
                    expectation.state = Expectation.State.SATISFIED
                    expectation.last_alerted_at = None
                elif expectation.due_at is not None and now > expectation.due_at + timedelta(
                    seconds=expectation.grace_s
                ):
                    newly_missed = expectation.state != Expectation.State.MISSED
                    if newly_missed:
                        expectation.state = Expectation.State.MISSED
                        expectation.miss_count += 1
                    due_at = expectation.due_at
                    should_alert = expectation.on_miss == Expectation.OnMiss.URGENT and (
                        expectation.last_alerted_at is None
                        or now - expectation.last_alerted_at >= URGENT_ALERT_COOLDOWN
                    )
                    if should_alert:
                        expectation.last_alerted_at = now
                        overdue_s = max(0, int((now - due_at).total_seconds()))
                        notices.append(
                            PendingNotice(
                                expectation_id=expectation.pk,
                                subject=f"Steward urgent: {expectation.subject}",
                                text=(
                                    f"Deadline missed. Miss count: {expectation.miss_count}. Overdue age: {overdue_s}s."
                                ),
                                fingerprint=(f"steward-miss:{expectation.pk}:{expectation.miss_count}"),
                            )
                        )
                    elif newly_missed and expectation.on_miss == Expectation.OnMiss.DIGEST:
                        digest_misses += 1
                current = (
                    expectation.state,
                    expectation.last_satisfied_at,
                    expectation.miss_count,
                    expectation.last_alerted_at,
                )
                if current != original:
                    changed.append(expectation)
                continue

            if latest is not None and (expectation.last_satisfied_at is None or latest > expectation.last_satisfied_at):
                expectation.last_satisfied_at = latest

            if expectation.last_satisfied_at is None or not expectation.interval_s:
                logger.warning(
                    "Steward interval expectation %s has no satisfaction baseline",
                    expectation.pk,
                )
                continue

            due_at = expectation.last_satisfied_at + timedelta(seconds=expectation.interval_s)
            missed = now > due_at + timedelta(seconds=expectation.grace_s)
            if missed:
                newly_missed = expectation.state != Expectation.State.MISSED
                if newly_missed:
                    expectation.state = Expectation.State.MISSED
                    expectation.miss_count += 1
                should_alert = expectation.on_miss == Expectation.OnMiss.URGENT and (
                    expectation.last_alerted_at is None or now - expectation.last_alerted_at >= URGENT_ALERT_COOLDOWN
                )
                if should_alert:
                    expectation.last_alerted_at = now
                    overdue_s = max(0, int((now - due_at).total_seconds()))
                    notices.append(
                        PendingNotice(
                            expectation_id=expectation.pk,
                            subject=f"Steward urgent: {expectation.subject}",
                            text=(
                                f"{expectation.kind.title()} missed. "
                                f"Miss count: {expectation.miss_count}. "
                                f"Overdue age: {overdue_s}s."
                            ),
                            fingerprint=(f"steward-miss:{expectation.pk}:{expectation.miss_count}"),
                        )
                    )
                elif newly_missed and expectation.on_miss == Expectation.OnMiss.DIGEST:
                    digest_misses += 1
            else:
                was_missed = expectation.state == Expectation.State.MISSED
                expectation.state = Expectation.State.ARMED
                if was_missed:
                    expectation.last_alerted_at = None
                    if (
                        expectation.kind == Expectation.Kind.HEARTBEAT
                        and expectation.on_miss == Expectation.OnMiss.URGENT
                    ):
                        evidence_age_s = max(
                            0,
                            int((now - expectation.last_satisfied_at).total_seconds()),
                        )
                        notices.append(
                            PendingNotice(
                                expectation_id=expectation.pk,
                                subject=f"Steward recovery: {expectation.subject}",
                                text=(
                                    f"Heartbeat resumed. Miss count: "
                                    f"{expectation.miss_count}. Evidence age: "
                                    f"{evidence_age_s}s."
                                ),
                                fingerprint=(
                                    f"steward-recovery:{expectation.pk}:{expectation.last_satisfied_at.isoformat()}"
                                ),
                            )
                        )

            current = (
                expectation.state,
                expectation.last_satisfied_at,
                expectation.miss_count,
                expectation.last_alerted_at,
            )
            if current != original:
                changed.append(expectation)

        if changed:
            Expectation.objects.bulk_update(
                changed,
                [
                    "state",
                    "last_satisfied_at",
                    "miss_count",
                    "last_alerted_at",
                ],
            )
    return notices, digest_misses


def _ping_deadman() -> None:
    url = getattr(settings, "STEWARD_DEADMAN_URL", "").strip()
    if not url:
        return
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error(
            "Steward dead-man ping failed error_class=%s",
            type(exc).__name__,
        )


def run_steward_sweep() -> dict[str, int]:
    """Evaluate the portfolio expectations and send direct urgent notices."""
    evaluated_at = timezone.now()
    notices, digest_misses = _claim_sweep_work(evaluated_at)
    delivered = 0
    for notice in notices:
        try:
            classification = send_urgent(
                notice.subject,
                notice.text,
                notice.fingerprint,
            )
        except Exception as exc:
            logger.error(
                "Steward urgent notifier raised expectation_id=%s error_class=%s",
                notice.expectation_id,
                type(exc).__name__,
            )
            classification = "transient"
        delivered += classification == "delivered"

    if digest_misses:
        logger.warning(
            "Steward sweep recorded %d new digest-class miss(es); no Phase 1 message sent",
            digest_misses,
        )
    _ping_deadman()
    from apps.steward.gate import should_send

    should_send("steward-sweep:liveness", timedelta(0))
    return {
        "notices": len(notices),
        "delivered": delivered,
        "digest_misses": digest_misses,
    }
