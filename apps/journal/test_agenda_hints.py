"""Tests for the cross-domain agenda-hint extractor (Phase C)."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from apps.journal.agenda_hints import _classify, run_agenda_hint_pass
from apps.tenants.agenda_models import AgendaEngagement
from apps.tenants.services import create_tenant


class HintPassEarlyExitTest(TestCase):
    """Cheap exit conditions — no LLM call should fire when there's
    nothing to classify. These guard against wasted token spend."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Early", telegram_chat_id=940001)

    def test_empty_content_returns_zero(self):
        with mock.patch("apps.journal.agenda_hints._classify") as mocked:
            result = run_agenda_hint_pass(self.tenant, "")
            self.assertEqual(result["matches"], 0)
            mocked.assert_not_called()

    def test_short_content_returns_zero(self):
        """Below the minimum chars threshold — skip the call."""
        with mock.patch("apps.journal.agenda_hints._classify") as mocked:
            result = run_agenda_hint_pass(self.tenant, "Hi")
            self.assertEqual(result["matches"], 0)
            mocked.assert_not_called()

    def test_no_open_threads_returns_zero(self):
        """Nothing to match against — skip the call."""
        # Tenant has no fuel_enabled, finance_enabled, no workouts/goals/plans
        with mock.patch("apps.journal.agenda_hints._classify") as mocked:
            result = run_agenda_hint_pass(self.tenant, "x" * 200)
            self.assertEqual(result["matches"], 0)
            mocked.assert_not_called()


class HintPassSignalCaptureTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Signals", telegram_chat_id=940010)
        self.tenant.fuel_enabled = True
        self.tenant.finance_enabled = True
        self.tenant.welcomes_sent = {}
        self.tenant.save()

    def test_warm_signal_recorded(self):
        """Classifier returns a warm match → record_signal fires →
        engagement row gains the warm entry."""
        with mock.patch(
            "apps.journal.agenda_hints._classify",
            return_value=[
                {"kind": "feature_intro", "item_id": "fuel", "signal": "warm"},
            ],
        ):
            summary = run_agenda_hint_pass(self.tenant, "I want to start running again." * 10)

        self.assertEqual(summary["matches"], 1)
        self.assertEqual(summary["warm"], 1)
        e = AgendaEngagement.objects.get(tenant=self.tenant, kind="feature_intro", item_id="fuel")
        signals = [s["signal"] for s in (e.response_signals or [])]
        self.assertIn("warm", signals)

    def test_classifier_failure_returns_zero_summary(self):
        """LLM call failure must not raise — main extraction is unaffected."""
        with mock.patch(
            "apps.journal.agenda_hints._classify",
            side_effect=RuntimeError("simulated openrouter outage"),
        ):
            summary = run_agenda_hint_pass(self.tenant, "x" * 200)
        self.assertEqual(summary["matches"], 0)

    def test_hallucinated_threads_dropped(self):
        """Classifier returns kind/item_id we didn't send — must be ignored
        rather than creating a phantom engagement row."""
        with mock.patch(
            "apps.journal.agenda_hints._classify",
            return_value=[
                {"kind": "feature_intro", "item_id": "imaginary_feature", "signal": "warm"},
            ],
        ):
            summary = run_agenda_hint_pass(self.tenant, "x" * 200)

        self.assertEqual(summary["matches"], 0)
        self.assertFalse(
            AgendaEngagement.objects.filter(
                tenant=self.tenant,
                item_id="imaginary_feature",
            ).exists()
        )

    def test_invalid_signal_dropped(self):
        """Unknown signal vocabulary → drop, don't write."""
        with mock.patch(
            "apps.journal.agenda_hints._classify",
            return_value=[
                {"kind": "feature_intro", "item_id": "fuel", "signal": "lukewarm"},
            ],
        ):
            summary = run_agenda_hint_pass(self.tenant, "x" * 200)
        self.assertEqual(summary["matches"], 0)

    def test_multiple_signals_classified(self):
        """A journal that mentions both fuel + finance with different
        sentiments produces two recorded signals."""
        with mock.patch(
            "apps.journal.agenda_hints._classify",
            return_value=[
                {"kind": "feature_intro", "item_id": "fuel", "signal": "warm"},
                {"kind": "feature_intro", "item_id": "finance", "signal": "redirect"},
            ],
        ):
            summary = run_agenda_hint_pass(self.tenant, "x" * 200)

        self.assertEqual(summary["matches"], 2)
        self.assertEqual(summary["warm"], 1)
        self.assertEqual(summary["redirect"], 1)


class ClassifyHttpFailureTest(TestCase):
    """The LLM call itself — verify error paths behave correctly. We
    don't test the *content* of the LLM response (that's an integration
    concern); we test that transport failures raise so the caller's
    try/except can catch."""

    def test_no_api_key_raises(self):
        from django.test import override_settings

        with override_settings(OPENROUTER_API_KEY=""), self.assertRaises(RuntimeError):
            _classify("content", [])
