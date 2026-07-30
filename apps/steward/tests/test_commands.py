from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.steward.models import EvidenceEvent, Expectation


class StewardCommandTests(TestCase):
    def test_seed_is_idempotent_and_preserves_operational_state(self):
        call_command("steward_seed_phase1", stdout=StringIO())
        heartbeat = Expectation.objects.get(
            kind=Expectation.Kind.HEARTBEAT,
            subject="personal-openclaw-gateway",
        )
        heartbeat.state = Expectation.State.MISSED
        heartbeat.miss_count = 2
        heartbeat.save(update_fields=["state", "miss_count"])

        call_command("steward_seed_phase1", stdout=StringIO())

        self.assertEqual(Expectation.objects.count(), 3)
        heartbeat.refresh_from_db()
        self.assertEqual(heartbeat.state, Expectation.State.MISSED)
        self.assertEqual(heartbeat.miss_count, 2)

    def test_ack_is_mj_provenance_satisfies_and_is_idempotent(self):
        call_command("steward_seed_phase1", stdout=StringIO())
        deadline = Expectation.objects.get(
            kind=Expectation.Kind.DEADLINE,
            subject="nbhd-ios-2.1.5-rollout",
        )

        call_command(
            "steward_ack",
            str(deadline.pk),
            note="rollout checked",
            stdout=StringIO(),
        )
        call_command(
            "steward_ack",
            str(deadline.pk),
            note="duplicate invocation",
            stdout=StringIO(),
        )

        deadline.refresh_from_db()
        self.assertEqual(deadline.state, Expectation.State.SATISFIED)
        event = EvidenceEvent.objects.get(source="mj_ack")
        self.assertEqual(event.provenance, EvidenceEvent.Provenance.MJ)
        self.assertEqual(event.payload, {"note": "rollout checked"})
