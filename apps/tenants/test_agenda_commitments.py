"""Tests for Phase D — assistant commitments + organic detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.agenda_models import AgendaEngagement
from apps.tenants.agenda_service import mark_organic, record_commitment
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


class RecordCommitmentTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Cmt", telegram_chat_id=950001)

    def test_creates_nascent_row_with_metadata(self):
        future = datetime.now(UTC) + timedelta(days=14)
        commitment = record_commitment(
            self.tenant,
            about="check in on debt progress",
            surface_after=future,
            why="user wanted to revisit in 2 weeks",
        )
        self.assertEqual(commitment.kind, AgendaEngagement.Kind.ASSISTANT_COMMITMENT)
        self.assertEqual(commitment.state, AgendaEngagement.State.NASCENT)
        self.assertEqual(commitment.metadata["about"], "check in on debt progress")
        self.assertEqual(commitment.metadata["why"], "user wanted to revisit in 2 weeks")
        self.assertEqual(commitment.surface_after, future)

    def test_idempotent_on_about_text_hash(self):
        """Same about-text → same item_id → same row reused, not duplicated."""
        future = datetime.now(UTC) + timedelta(days=14)
        c1 = record_commitment(
            self.tenant,
            about="check in on debt",
            surface_after=future,
            why="first reasoning",
        )
        c2 = record_commitment(
            self.tenant,
            about="check in on debt",
            surface_after=future + timedelta(days=1),
            why="updated reasoning",
        )
        self.assertEqual(c1.item_id, c2.item_id)
        self.assertEqual(
            AgendaEngagement.objects.filter(
                tenant=self.tenant,
                kind=AgendaEngagement.Kind.ASSISTANT_COMMITMENT,
            ).count(),
            1,
        )
        # Latest call's metadata wins.
        c2.refresh_from_db()
        self.assertEqual(c2.metadata["why"], "updated reasoning")

    def test_explicit_item_id_overrides_hash(self):
        future = datetime.now(UTC) + timedelta(days=14)
        commitment = record_commitment(
            self.tenant,
            about="some topic",
            surface_after=future,
            why="why",
            item_id="explicit-id-here",
        )
        self.assertEqual(commitment.item_id, "explicit-id-here")


class MarkOrganicTest(TestCase):
    def test_transitions_to_active_and_logs_signal(self):
        tenant = create_tenant(display_name="Org", telegram_chat_id=950002)
        future = datetime.now(UTC) + timedelta(days=14)
        record_commitment(
            tenant,
            about="check in on debt",
            surface_after=future,
            why="reasoning",
        )

        # Find the row by (tenant, kind=commitment)
        row = AgendaEngagement.objects.get(
            tenant=tenant,
            kind=AgendaEngagement.Kind.ASSISTANT_COMMITMENT,
        )
        mark_organic(tenant, kind=row.kind, item_id=row.item_id)
        row.refresh_from_db()
        self.assertEqual(row.state, AgendaEngagement.State.ACTIVE)
        signals = [s["signal"] for s in (row.response_signals or [])]
        self.assertIn("organic", signals)


class RuntimeCommitmentEndpointTest(TestCase):
    def setUp(self):
        from django.test import override_settings

        self.tenant = create_tenant(display_name="EndpointD", telegram_chat_id=950010)
        seed_internal_key(self.tenant, key="test-internal-key")
        self.client = APIClient()
        self._override = override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
        self._override.enable()

    def tearDown(self):
        self._override.disable()

    def _post(self, body):
        from django.urls import reverse

        url = reverse("runtime-commitment-record", kwargs={"tenant_id": self.tenant.id})
        return self.client.post(
            url,
            body,
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

    def test_creates_commitment(self):
        future = (datetime.now(UTC) + timedelta(days=14)).isoformat()
        resp = self._post({"about": "the topic", "surface_after": future, "why": "the reason"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], AgendaEngagement.Kind.ASSISTANT_COMMITMENT)
        self.assertTrue(body["item_id"])

        row = AgendaEngagement.objects.get(tenant=self.tenant, kind=body["kind"], item_id=body["item_id"])
        self.assertEqual(row.metadata["about"], "the topic")
        self.assertEqual(row.metadata["why"], "the reason")

    def test_missing_about_400(self):
        future = (datetime.now(UTC) + timedelta(days=14)).isoformat()
        resp = self._post({"surface_after": future, "why": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_missing_surface_after_400(self):
        resp = self._post({"about": "topic", "why": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_surface_after_400(self):
        resp = self._post({"about": "topic", "why": "x", "surface_after": "not-a-date"})
        self.assertEqual(resp.status_code, 400)


class CommitmentRendererTest(TestCase):
    """Phase D renderer surfaces commitments past surface_after in
    NASCENT/INTRODUCED state — and only those."""

    def setUp(self):
        from apps.orchestrator.agenda_envelope import _render_assistant_commitments

        self._render = _render_assistant_commitments
        self.tenant = create_tenant(display_name="CmtRender", telegram_chat_id=950020)

    def test_empty_when_no_commitments(self):
        self.assertEqual(self._render(self.tenant), "")

    def test_renders_past_due_nascent_commitment(self):
        past = datetime.now(UTC) - timedelta(days=1)
        record_commitment(
            self.tenant,
            about="check in on debt",
            surface_after=past,
            why="user wanted breathing room",
        )
        out = self._render(self.tenant)
        self.assertIn("check in on debt", out)
        self.assertIn("breathing room", out)

    def test_skips_future_surface_after(self):
        """A commitment whose surface_after is still in the future
        shouldn't render (the assistant doesn't know about it yet)."""
        future = datetime.now(UTC) + timedelta(days=14)
        record_commitment(
            self.tenant,
            about="ask about marathon training",
            surface_after=future,
            why="user said they'd start in 2 weeks",
        )
        self.assertEqual(self._render(self.tenant), "")

    def test_skips_active_state(self):
        """Once the user organically raised the topic, commitment is
        ACTIVE — assistant supports rather than introduces, so the
        commitment drops out of the surfacing list."""
        past = datetime.now(UTC) - timedelta(days=1)
        record_commitment(
            self.tenant,
            about="check in on debt",
            surface_after=past,
            why="reasoning",
        )
        row = AgendaEngagement.objects.get(
            tenant=self.tenant,
            kind=AgendaEngagement.Kind.ASSISTANT_COMMITMENT,
        )
        mark_organic(self.tenant, kind=row.kind, item_id=row.item_id)
        self.assertEqual(self._render(self.tenant), "")


class HintExtractorOrganicCommitmentTest(TestCase):
    """Phase C × Phase D: an organic signal on an ASSISTANT_COMMITMENT
    auto-transitions the commitment to ACTIVE."""

    def test_organic_signal_marks_commitment_active(self):
        from apps.journal.agenda_hints import run_agenda_hint_pass

        tenant = create_tenant(display_name="OrgCmt", telegram_chat_id=950030)
        past = datetime.now(UTC) - timedelta(days=1)
        commitment = record_commitment(
            tenant,
            about="ask about marathon training",
            surface_after=past,
            why="user wanted to revisit",
        )
        # Simulate the classifier matching this commitment as 'organic'
        with mock.patch(
            "apps.journal.agenda_hints._classify",
            return_value=[
                {
                    "kind": AgendaEngagement.Kind.ASSISTANT_COMMITMENT,
                    "item_id": commitment.item_id,
                    "signal": "organic",
                },
            ],
        ):
            run_agenda_hint_pass(tenant, "x" * 200)

        commitment.refresh_from_db()
        self.assertEqual(commitment.state, AgendaEngagement.State.ACTIVE)
