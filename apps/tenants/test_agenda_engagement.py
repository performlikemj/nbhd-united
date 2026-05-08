"""Tests for AgendaEngagement (Phase B): model, service, signal, endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.agenda_models import AgendaEngagement
from apps.tenants.agenda_service import (
    defer_until,
    engagements_by_item,
    is_eligible_now,
    mark_state,
    mark_surfaced,
    record_signal,
)
from apps.tenants.services import create_tenant


class AgendaServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Eng", telegram_chat_id=930001)

    def test_mark_surfaced_creates_row_and_advances_state(self):
        e = mark_surfaced(self.tenant, kind="feature_intro", item_id="fuel")
        self.assertEqual(e.state, AgendaEngagement.State.INTRODUCED)
        self.assertIsNotNone(e.last_surfaced_at)

    def test_mark_surfaced_idempotent(self):
        first = mark_surfaced(self.tenant, kind="feature_intro", item_id="fuel")
        second = mark_surfaced(self.tenant, kind="feature_intro", item_id="fuel")
        self.assertEqual(first.pk, second.pk)
        # Two surfaces, one row.
        self.assertEqual(AgendaEngagement.objects.filter(tenant=self.tenant).count(), 1)

    def test_mark_surfaced_does_not_clobber_explicit_state(self):
        """If state has already been set to COMPLETED/ABANDONED, a fresh
        surface shouldn't drop it back down to INTRODUCED."""
        mark_surfaced(self.tenant, kind="feature_intro", item_id="fuel")
        mark_state(self.tenant, kind="feature_intro", item_id="fuel", state="completed")
        again = mark_surfaced(self.tenant, kind="feature_intro", item_id="fuel")
        # Last surface was updated…
        self.assertIsNotNone(again.last_surfaced_at)
        # …but state is preserved (mark_surfaced only auto-advances NASCENT)
        self.assertEqual(again.state, AgendaEngagement.State.COMPLETED)

    def test_record_signal_appends_log(self):
        record_signal(self.tenant, kind="feature_intro", item_id="fuel", signal="warm")
        record_signal(self.tenant, kind="feature_intro", item_id="fuel", signal="ignore")
        e = AgendaEngagement.objects.get(tenant=self.tenant, kind="feature_intro", item_id="fuel")
        self.assertEqual(len(e.response_signals), 2)
        self.assertEqual(e.response_signals[0]["signal"], "warm")
        self.assertEqual(e.response_signals[1]["signal"], "ignore")

    def test_mark_state_validates(self):
        with self.assertRaises(ValueError):
            mark_state(self.tenant, kind="feature_intro", item_id="fuel", state="totally-made-up")

    def test_defer_until(self):
        future = datetime.now(UTC) + timedelta(days=14)
        defer_until(self.tenant, kind="feature_intro", item_id="fuel", when=future)
        e = AgendaEngagement.objects.get(tenant=self.tenant, kind="feature_intro", item_id="fuel")
        self.assertEqual(e.surface_after, future)


class IsEligibleNowTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Elig", telegram_chat_id=930002)

    def _row(self, **fields) -> AgendaEngagement:
        return AgendaEngagement.objects.create(
            tenant=self.tenant,
            kind="feature_intro",
            item_id="fuel",
            **fields,
        )

    def test_no_row_eligible(self):
        self.assertTrue(is_eligible_now(None))

    def test_abandoned_not_eligible(self):
        e = self._row(state="abandoned")
        self.assertFalse(is_eligible_now(e))

    def test_completed_not_eligible(self):
        e = self._row(state="completed")
        self.assertFalse(is_eligible_now(e))

    def test_recently_surfaced_not_eligible(self):
        e = self._row(last_surfaced_at=datetime.now(UTC) - timedelta(minutes=30))
        self.assertFalse(is_eligible_now(e))

    def test_old_surface_eligible(self):
        e = self._row(last_surfaced_at=datetime.now(UTC) - timedelta(hours=24))
        self.assertTrue(is_eligible_now(e))

    def test_surface_after_future_not_eligible(self):
        e = self._row(surface_after=datetime.now(UTC) + timedelta(days=7))
        self.assertFalse(is_eligible_now(e))

    def test_surface_after_past_eligible(self):
        e = self._row(surface_after=datetime.now(UTC) - timedelta(days=1))
        self.assertTrue(is_eligible_now(e))

    def test_recent_redirect_signal_suppresses(self):
        """Phase C: a recent redirect signal in response_signals
        suppresses the thread for the redirect-suppress window."""
        recent = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        e = self._row(response_signals=[{"at": recent, "signal": "redirect"}])
        self.assertFalse(is_eligible_now(e))

    def test_recent_ignore_signal_suppresses(self):
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        e = self._row(response_signals=[{"at": recent, "signal": "ignore"}])
        self.assertFalse(is_eligible_now(e))

    def test_old_redirect_signal_does_not_suppress(self):
        """Past the redirect window — eligibility restored."""
        old = (datetime.now(UTC) - timedelta(days=14)).isoformat()
        e = self._row(response_signals=[{"at": old, "signal": "redirect"}])
        self.assertTrue(is_eligible_now(e))

    def test_warm_signal_after_redirect_clears_suppression(self):
        """If the user re-engaged after pushing back, suppression lifts."""
        old_redirect = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        recent_warm = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
        e = self._row(
            response_signals=[
                {"at": old_redirect, "signal": "redirect"},
                {"at": recent_warm, "signal": "warm"},
            ],
        )
        self.assertTrue(is_eligible_now(e))

    def test_malformed_signal_entries_skipped(self):
        """Defensive: garbage in the JSON log doesn't crash the filter."""
        e = self._row(
            response_signals=[
                {"signal": "redirect"},  # missing 'at'
                "not-a-dict",
                {"at": "not-a-date", "signal": "redirect"},
            ],
        )
        # Nothing parseable → no constraint applied → eligible
        self.assertTrue(is_eligible_now(e))


class EngagementsByItemTest(TestCase):
    def test_returns_dict_keyed_by_item_id(self):
        tenant = create_tenant(display_name="Bulk", telegram_chat_id=930003)
        AgendaEngagement.objects.create(tenant=tenant, kind="feature_intro", item_id="fuel", state="active")
        AgendaEngagement.objects.create(tenant=tenant, kind="feature_intro", item_id="finance", state="abandoned")
        # Different kind — must be excluded
        AgendaEngagement.objects.create(tenant=tenant, kind="payoff_plan", item_id="fuel", state="active")

        result = engagements_by_item(tenant, kind="feature_intro")
        self.assertEqual(set(result.keys()), {"fuel", "finance"})
        self.assertEqual(result["fuel"].state, "active")
        self.assertEqual(result["finance"].state, "abandoned")


class WelcomesSentMirrorTest(TestCase):
    """When ``Tenant.welcomes_sent`` flips null → timestamp, the matching
    AgendaEngagement row should be marked COMPLETED with
    last_surfaced_at = the timestamp."""

    def test_setting_welcomes_creates_completed_engagement(self):
        tenant = create_tenant(display_name="Mirror", telegram_chat_id=930010)
        tenant.welcomes_sent = {}
        tenant.save()
        # No engagement yet
        self.assertEqual(AgendaEngagement.objects.filter(tenant=tenant).count(), 0)

        ts = "2026-05-08T12:00:00+00:00"
        tenant.welcomes_sent = {"fuel": ts}
        tenant.save()

        e = AgendaEngagement.objects.get(tenant=tenant, kind="feature_intro", item_id="fuel")
        self.assertEqual(e.state, AgendaEngagement.State.COMPLETED)
        self.assertEqual(e.last_surfaced_at.isoformat(), datetime.fromisoformat(ts).isoformat())

    def test_existing_welcomes_dont_re_fire(self):
        """A no-op save (welcomes_sent already set) shouldn't trigger
        another mirror, since it's not a fresh transition."""
        tenant = create_tenant(display_name="NoOp", telegram_chat_id=930011)
        tenant.welcomes_sent = {"fuel": "2026-05-08T12:00:00+00:00"}
        tenant.save()
        # First save creates the engagement row.
        self.assertEqual(AgendaEngagement.objects.filter(tenant=tenant).count(), 1)

        # Now mark it ABANDONED to verify the next save doesn't clobber.
        mark_state(tenant, kind="feature_intro", item_id="fuel", state="abandoned")

        # Touch the tenant — same welcomes_sent.
        tenant.display_name = "NoOp Renamed"
        tenant.save()

        e = AgendaEngagement.objects.get(tenant=tenant, kind="feature_intro", item_id="fuel")
        # State stayed ABANDONED — the mirror didn't re-fire.
        self.assertEqual(e.state, AgendaEngagement.State.ABANDONED)


class RuntimeAgendaEngagementEndpointTest(TestCase):
    """Smoke test for the runtime endpoint. Auth is exercised by the
    welcome endpoint's tests; here we just verify dispatch + state
    transitions wire up correctly."""

    def setUp(self):
        from django.test import override_settings

        self.tenant = create_tenant(display_name="Endpoint", telegram_chat_id=930020)
        self.client = APIClient()
        self._override = override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
        self._override.enable()

    def tearDown(self):
        self._override.disable()

    def _post(self, kind: str, item_id: str, body: dict) -> object:
        from django.urls import reverse

        url = reverse(
            "runtime-agenda-engagement",
            kwargs={"tenant_id": self.tenant.id, "kind": kind, "item_id": item_id},
        )
        return self.client.post(
            url,
            body,
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

    def test_surfaced_action_creates_row(self):
        resp = self._post("feature_intro", "fuel", {"action": "surfaced"})
        self.assertEqual(resp.status_code, 200)
        e = AgendaEngagement.objects.get(tenant=self.tenant, kind="feature_intro", item_id="fuel")
        self.assertEqual(e.state, AgendaEngagement.State.INTRODUCED)
        self.assertIsNotNone(e.last_surfaced_at)

    def test_abandoned_action_sets_state(self):
        resp = self._post("feature_intro", "fuel", {"action": "abandoned"})
        self.assertEqual(resp.status_code, 200)
        e = AgendaEngagement.objects.get(tenant=self.tenant, kind="feature_intro", item_id="fuel")
        self.assertEqual(e.state, AgendaEngagement.State.ABANDONED)

    def test_defer_requires_until(self):
        resp = self._post("feature_intro", "fuel", {"action": "defer"})
        self.assertEqual(resp.status_code, 400)

    def test_defer_with_until(self):
        future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        resp = self._post("feature_intro", "fuel", {"action": "defer", "until": future})
        self.assertEqual(resp.status_code, 200)
        e = AgendaEngagement.objects.get(tenant=self.tenant, kind="feature_intro", item_id="fuel")
        self.assertIsNotNone(e.surface_after)

    def test_unknown_kind_400(self):
        resp = self._post("not-a-kind", "fuel", {"action": "surfaced"})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_action_400(self):
        resp = self._post("feature_intro", "fuel", {"action": "totally-fake"})
        self.assertEqual(resp.status_code, 400)
