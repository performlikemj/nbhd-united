from datetime import UTC, datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.steward.models import EvidenceSource, Expectation


class Command(BaseCommand):
    help = "Idempotently seed the three Steward Phase 1 expectations."

    def handle(self, *args, **options):
        now = timezone.now()
        seeds = [
            {
                "kind": Expectation.Kind.HEARTBEAT,
                "subject": "personal-openclaw-gateway",
                "spec": {
                    "interval_s": 1800,
                    "due_at": None,
                    "cron_expr": None,
                    "grace_s": 900,
                    "evidence_source": EvidenceSource.GATEWAY_HEARTBEAT,
                    "on_miss": Expectation.OnMiss.URGENT,
                    "owner": "mj",
                },
                "initial_satisfied_at": now,
            },
            {
                "kind": Expectation.Kind.DEADLINE,
                "subject": "nbhd-ios-2.1.5-rollout",
                "spec": {
                    "interval_s": None,
                    "due_at": datetime(2026, 8, 6, tzinfo=UTC),
                    "cron_expr": None,
                    "grace_s": 86400,
                    "evidence_source": EvidenceSource.ASC_VERSION_STATE,
                    "on_miss": Expectation.OnMiss.DIGEST,
                    "owner": "mj",
                },
                "initial_satisfied_at": None,
            },
            {
                "kind": Expectation.Kind.RECURRENCE,
                "subject": "nbhd-united-main-ci",
                "spec": {
                    "interval_s": 604800,
                    "due_at": None,
                    "cron_expr": None,
                    "grace_s": 86400,
                    "evidence_source": EvidenceSource.CI_RUN,
                    "on_miss": Expectation.OnMiss.DIGEST,
                    "owner": "mj",
                },
                "initial_satisfied_at": now,
            },
        ]

        for seed in seeds:
            expectation, created = Expectation.objects.get_or_create(
                kind=seed["kind"],
                subject=seed["subject"],
                defaults={
                    **seed["spec"],
                    "state": Expectation.State.ARMED,
                    "last_satisfied_at": seed["initial_satisfied_at"],
                },
            )
            if not created:
                changed = []
                for field, value in seed["spec"].items():
                    if getattr(expectation, field) != value:
                        setattr(expectation, field, value)
                        changed.append(field)
                if (
                    expectation.kind in (Expectation.Kind.HEARTBEAT, Expectation.Kind.RECURRENCE)
                    and expectation.last_satisfied_at is None
                ):
                    expectation.last_satisfied_at = seed["initial_satisfied_at"]
                    changed.append("last_satisfied_at")
                if changed:
                    expectation.full_clean()
                    expectation.save(update_fields=changed)
            action = "created" if created else "present"
            self.stdout.write(f"{action}: {expectation.kind}:{expectation.subject}")

        self.stdout.write(self.style.SUCCESS("Steward Phase 1 expectations seeded."))
