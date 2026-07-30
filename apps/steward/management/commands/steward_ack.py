from __future__ import annotations

import logging
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.steward.models import EvidenceEvent, EvidenceSource, Expectation
from apps.steward.notify import send_urgent
from apps.steward.services import ingest_evidence, validate_payload_size

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Record an MJ acknowledgement and satisfy one Steward expectation."

    def add_arguments(self, parser):
        parser.add_argument("expectation_id", type=int)
        parser.add_argument(
            "--note",
            default="",
            help="Optional short operational note (do not include user PII).",
        )

    def handle(self, *args, **options):
        try:
            expectation = Expectation.objects.get(pk=options["expectation_id"])
        except Expectation.DoesNotExist as exc:
            raise CommandError("Expectation does not exist.") from exc
        if expectation.state == Expectation.State.RETIRED:
            raise CommandError("A retired expectation cannot be acknowledged.")
        if expectation.state == Expectation.State.SATISFIED:
            self.stdout.write(
                self.style.SUCCESS(f"Expectation {expectation.pk} is already satisfied; no new evidence recorded.")
            )
            return

        payload = {"note": options["note"]} if options["note"] else {}
        try:
            validate_payload_size(payload)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        now = timezone.now()
        result = ingest_evidence(
            source=EvidenceSource.MJ_ACK,
            subject=expectation.subject,
            occurred_at=now,
            payload=payload,
            fingerprint=f"mj-ack:{expectation.pk}:{uuid.uuid4().hex}",
            trust=EvidenceEvent.Trust.AUTHENTICATED_API,
            provenance=EvidenceEvent.Provenance.MJ,
            now=now,
            expectation_ids=(expectation.pk,),
        )
        for recovered in result.recovery_expectations:
            try:
                send_urgent(
                    subject=f"Steward recovery: {recovered.subject}",
                    text=(f"Heartbeat acknowledged by MJ. Miss count: {recovered.miss_count}. Evidence age: 0s."),
                    fingerprint=f"steward-recovery:{recovered.pk}:{now.isoformat()}",
                )
            except Exception as exc:
                logger.error(
                    "Steward recovery notifier raised expectation_id=%s error_class=%s",
                    recovered.pk,
                    type(exc).__name__,
                )
        self.stdout.write(
            self.style.SUCCESS(f"Acknowledged expectation {expectation.pk} with evidence {result.event.pk}.")
        )
