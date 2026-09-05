from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.steward.models import Decision, EvidenceEvent, EvidenceSource, Expectation, TrackedItem


class StewardCommandTests(TestCase):
    def test_expect_heartbeat_is_idempotent_and_preserves_baseline(self):
        arguments = (
            "steward_expect_heartbeat",
            "--subject",
            "basecamp-pm",
            "--interval",
            "1800",
            "--grace",
            "900",
            "--title",
            "Basecamp PM flow",
        )
        call_command(*arguments, stdout=StringIO())
        expectation = Expectation.objects.get(subject="basecamp-pm")
        baseline = expectation.last_satisfied_at

        call_command(*arguments, stdout=StringIO())

        self.assertEqual(TrackedItem.objects.count(), 1)
        self.assertEqual(Expectation.objects.count(), 1)
        item = TrackedItem.objects.get()
        expectation.refresh_from_db()
        self.assertEqual(item.product, TrackedItem.Product.PORTFOLIO)
        self.assertEqual(item.kind, TrackedItem.Kind.INFRA_WATCH)
        self.assertEqual(item.title, "Basecamp PM flow")
        self.assertEqual(item.status, TrackedItem.Status.ACTIVE)
        self.assertEqual(expectation.kind, Expectation.Kind.HEARTBEAT)
        self.assertEqual(expectation.interval_s, 1800)
        self.assertEqual(expectation.grace_s, 900)
        self.assertEqual(expectation.evidence_source, EvidenceSource.GATEWAY_HEARTBEAT)
        self.assertEqual(expectation.on_miss, Expectation.OnMiss.URGENT)
        self.assertEqual(expectation.subject_item_id, item.pk)
        self.assertEqual(expectation.last_satisfied_at, baseline)

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

    def test_phase2_seed_is_idempotent_and_links_phase1_items(self):
        call_command("steward_seed_phase1", stdout=StringIO())
        call_command("steward_seed_phase2", stdout=StringIO())
        policy_expectation = Expectation.objects.get(subject="decision:eval-slo-policy")
        due_at = policy_expectation.due_at

        call_command("steward_seed_phase2", stdout=StringIO())

        self.assertEqual(TrackedItem.objects.count(), 4)
        self.assertEqual(Expectation.objects.count(), 4)
        policy_expectation.refresh_from_db()
        self.assertEqual(policy_expectation.due_at, due_at)
        self.assertIsNotNone(policy_expectation.subject_item_id)
        for subject in (
            "personal-openclaw-gateway",
            "nbhd-ios-2.1.5-rollout",
            "nbhd-united-main-ci",
        ):
            self.assertIsNotNone(Expectation.objects.get(subject=subject).subject_item_id)

    def test_item_create_update_and_decision_close(self):
        call_command(
            "steward_item",
            "--title",
            "Release train",
            "--product",
            "nbhd_ios",
            "--kind",
            "release",
            "--status",
            "active",
            "--ref",
            "pr=104",
            stdout=StringIO(),
        )
        call_command(
            "steward_item",
            "--title",
            "Release train",
            "--product",
            "nbhd_ios",
            "--kind",
            "release",
            "--context",
            "Verified",
            stdout=StringIO(),
        )
        item = TrackedItem.objects.get()
        self.assertEqual(item.context, "Verified")
        self.assertEqual(item.refs, [{"type": "pr", "value": "104"}])
        self.assertEqual(item.provenance, EvidenceEvent.Provenance.MJ)

        call_command(
            "steward_decide",
            "--decision",
            "Close the train",
            "--rationale",
            "Released",
            "--item",
            str(item.id),
            stdout=StringIO(),
        )
        item.refresh_from_db()
        self.assertEqual(item.status, TrackedItem.Status.DONE)
        self.assertEqual(Decision.objects.count(), 1)
