"""Tests for the free-model offer resolver + health-state helpers."""

from django.test import TestCase

from apps.billing.constants import DEEPSEEK_MODEL, NEMOTRON_FREE_MODEL
from apps.billing.model_offers import (
    _pricing_is_free,
    offer_is_active,
    offer_model_entry,
    offer_state,
    record_model_failure,
    record_model_pricing,
    record_model_success,
    resolve_default_primary_model,
)
from apps.billing.models import FreeModelOffer, ModelHealth


class PricingIsFreeTest(TestCase):
    def test_zero_strings_are_free(self):
        self.assertTrue(_pricing_is_free({"prompt": "0", "completion": "0"}))

    def test_nonzero_is_not_free(self):
        self.assertFalse(_pricing_is_free({"prompt": "0.0000004", "completion": "0"}))

    def test_empty_is_not_free(self):
        self.assertFalse(_pricing_is_free({}))

    def test_garbage_is_not_free(self):
        self.assertFalse(_pricing_is_free({"prompt": "n/a", "completion": "n/a"}))


class ResolverTest(TestCase):
    def test_default_inactive_offer(self):
        # Singleton defaults to inactive.
        self.assertFalse(offer_is_active())
        self.assertEqual(offer_model_entry(), {})
        self.assertEqual(resolve_default_primary_model(DEEPSEEK_MODEL), DEEPSEEK_MODEL)

    def test_active_offer_overrides_default(self):
        offer = FreeModelOffer.load()
        offer.is_active = True
        offer.save(update_fields=["is_active"])
        self.assertTrue(offer_is_active())
        self.assertIn(NEMOTRON_FREE_MODEL, offer_model_entry())
        self.assertEqual(resolve_default_primary_model(DEEPSEEK_MODEL), NEMOTRON_FREE_MODEL)

    def test_kill_switch_forces_inactive(self):
        offer = FreeModelOffer.load()
        offer.is_active = True
        offer.enabled = False
        offer.save(update_fields=["is_active", "enabled"])
        self.assertFalse(offer_is_active())
        self.assertEqual(resolve_default_primary_model(DEEPSEEK_MODEL), DEEPSEEK_MODEL)


class HealthStateWriterTest(TestCase):
    def test_record_success_resets_failures(self):
        record_model_failure(DEEPSEEK_MODEL, "boom")
        record_model_failure(DEEPSEEK_MODEL, "boom")
        self.assertEqual(ModelHealth.objects.get(model_id=DEEPSEEK_MODEL).consecutive_failures, 2)
        record_model_success(DEEPSEEK_MODEL)
        row = ModelHealth.objects.get(model_id=DEEPSEEK_MODEL)
        self.assertEqual(row.consecutive_failures, 0)
        self.assertTrue(row.is_reachable)
        self.assertIsNotNone(row.last_ok_at)

    def test_record_pricing_sets_free_flag(self):
        self.assertTrue(record_model_pricing(NEMOTRON_FREE_MODEL, {"prompt": "0", "completion": "0"}))
        self.assertTrue(ModelHealth.objects.get(model_id=NEMOTRON_FREE_MODEL).is_free)
        self.assertFalse(record_model_pricing(NEMOTRON_FREE_MODEL, {"prompt": "0.01", "completion": "0.02"}))
        self.assertFalse(ModelHealth.objects.get(model_id=NEMOTRON_FREE_MODEL).is_free)


class OfferStateTest(TestCase):
    def test_shape(self):
        state = offer_state()
        for key in ("active", "model_id", "fallback_model_id", "health"):
            self.assertIn(key, state)
        self.assertEqual(state["model_id"], NEMOTRON_FREE_MODEL)
        self.assertEqual(state["fallback_model_id"], DEEPSEEK_MODEL)
        self.assertFalse(state["active"])
